"""Passkey 登录与当前用户的通行密钥管理。"""
import json
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    parse_authentication_credential_json,
    parse_client_data_json,
    parse_registration_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app import webauthn_service as service
from app.api.auth import complete_login
from app.auth import get_current_user, verify_password
from app.database import get_db
from app.database_models import PasskeyModel, UserModel
from app.models import Token

router = APIRouter()
VERIFICATION_ERRORS = (WebAuthnException, ValueError, TypeError, KeyError, OverflowError)


class PasswordConfirmation(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class CredentialVerification(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=64)
    credential: dict

    @field_validator("credential")
    @classmethod
    def limit_credential_size(cls, value):
        if len(json.dumps(value)) > 131072:
            raise ValueError("Passkey 响应过大")
        return value


class RegistrationVerification(CredentialVerification):
    name: str = Field(default="Passkey", min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请输入 Passkey 名称")
        return value


class PasskeyResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None
    backed_up: bool


def _metadata(passkey):
    return PasskeyResponse(
        id=passkey.id,
        name=passkey.name,
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
        backed_up=passkey.backed_up,
    )


def _require_approved(user):
    if not user.approved and user.role != "admin":
        raise HTTPException(status_code=403, detail="账号尚未通过管理员审批，请联系管理员")


def _confirm_password(user, password):
    _require_approved(user)
    if not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=400, detail="当前密码不正确")


def _check_capacity(db, user_id):
    if db.query(PasskeyModel).filter(PasskeyModel.user_id == user_id).count() >= service.MAX_PASSKEYS_PER_USER:
        raise HTTPException(status_code=400, detail="最多可绑定 20 个 Passkey，请先删除不再使用的密钥")


def _reject_cross_origin(client_data_json):
    if parse_client_data_json(client_data_json).cross_origin:
        raise ValueError("Cross-origin WebAuthn ceremonies are not allowed")


@router.get("", response_model=list[PasskeyResponse])
def list_passkeys(user=Depends(get_current_user), db=Depends(get_db)):
    _require_approved(user)
    passkeys = db.query(PasskeyModel).filter(PasskeyModel.user_id == user.id).order_by(
        PasskeyModel.created_at.desc(), PasskeyModel.id.desc(),
    ).all()
    return [_metadata(passkey) for passkey in passkeys]


@router.post("/register/options")
def registration_options(body: PasswordConfirmation, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    service.limit_request(request, "register/options", limit=10)
    origin = service.request_origin(request)
    _confirm_password(user, body.password)
    _check_capacity(db, user.id)
    user_handle = service.ensure_user_handle(db, user)
    existing = db.query(PasskeyModel).filter(PasskeyModel.user_id == user.id, PasskeyModel.rp_id == service.WEBAUTHN_RP_ID).all()
    options = generate_registration_options(
        rp_id=service.WEBAUTHN_RP_ID,
        rp_name=service.WEBAUTHN_RP_NAME,
        user_id=user_handle,
        user_name=user.username,
        user_display_name=user.display_name or user.username,
        timeout=60000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(passkey.credential_id)) for passkey in existing],
    )
    return service.create_challenge(db, options, "register", origin, user.id)


@router.post("/register/verify", response_model=PasskeyResponse, status_code=201)
def registration_verify(body: RegistrationVerification, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    service.limit_request(request, "register/verify")
    _require_approved(user)
    origin = service.request_origin(request)
    ceremony = service.consume_challenge(db, body.challenge_id, "register", origin, user.id)
    try:
        credential = parse_registration_credential_json(body.credential)
        _reject_cross_origin(credential.response.client_data_json)
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=ceremony.challenge,
            expected_rp_id=ceremony.rp_id,
            expected_origin=ceremony.origin,
            require_user_verification=True,
        )
        if verified.credential_id != credential.raw_id:
            raise ValueError("Credential IDs do not match")
    except VERIFICATION_ERRORS:
        raise HTTPException(status_code=400, detail="Passkey 绑定验证失败，请重新绑定") from None
    # 在实际写入前锁定用户：SQLite 的写事务与 PostgreSQL 的行锁都能串行化
    # 同一用户的容量检查/INSERT，避免并发绑定穿透 20 个密钥的上限。
    locked = db.query(UserModel).filter(UserModel.id == user.id).update(
        {UserModel.webauthn_user_handle: UserModel.webauthn_user_handle}, synchronize_session=False,
    )
    if locked != 1:
        db.rollback()
        raise HTTPException(status_code=401, detail="账号已不存在，请重新登录")
    db.refresh(user)
    _require_approved(user)
    _check_capacity(db, user.id)
    credential_id = bytes_to_base64url(verified.credential_id)
    if db.query(PasskeyModel).filter(PasskeyModel.credential_id == credential_id).first():
        raise HTTPException(status_code=409, detail="此 Passkey 已绑定，请使用其他通行密钥")
    passkey = PasskeyModel(
        user_id=user.id,
        credential_id=credential_id,
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        rp_id=ceremony.rp_id,
        name=body.name,
        transports=json.dumps([transport.value for transport in credential.response.transports or []]),
        device_type=verified.credential_device_type.value,
        backed_up=verified.credential_backed_up,
    )
    db.add(passkey)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="此 Passkey 已绑定或账号已不存在，请重新登录后重试") from None
    db.refresh(passkey)
    return _metadata(passkey)


@router.post("/login/options")
def login_options(request: Request, db=Depends(get_db)):
    service.limit_request(request, "login/options")
    origin = service.request_origin(request)
    # 不提供 allowCredentials：由可发现的 Passkey 选择账户，不暴露用户名或凭据列表。
    options = generate_authentication_options(
        rp_id=service.WEBAUTHN_RP_ID,
        timeout=60000,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return service.create_challenge(db, options, "login", origin)


@router.post("/login/verify", response_model=Token)
def login_verify(body: CredentialVerification, request: Request, db=Depends(get_db)):
    service.limit_request(request, "login/verify")
    origin = service.request_origin(request)
    ceremony = service.consume_challenge(db, body.challenge_id, "login", origin)
    try:
        credential = parse_authentication_credential_json(body.credential)
        _reject_cross_origin(credential.response.client_data_json)
        passkey = db.query(PasskeyModel).filter(
            PasskeyModel.credential_id == credential.id, PasskeyModel.rp_id == ceremony.rp_id,
        ).first()
        if passkey is None:
            raise ValueError("Unknown credential")
        user = db.query(UserModel).filter(UserModel.id == passkey.user_id).first()
        user_handle = credential.response.user_handle
        if user is None or not user.webauthn_user_handle or not user_handle:
            raise ValueError("Missing user handle")
        if not secrets.compare_digest(user_handle, base64url_to_bytes(user.webauthn_user_handle)):
            raise ValueError("Wrong user handle")
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=ceremony.challenge,
            expected_rp_id=ceremony.rp_id,
            expected_origin=ceremony.origin,
            credential_public_key=base64url_to_bytes(passkey.public_key),
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
        if verified.credential_device_type.value != passkey.device_type:
            raise ValueError("Backup eligibility changed")
    except VERIFICATION_ERRORS:
        raise HTTPException(status_code=401, detail="Passkey 登录验证失败，请重试或使用密码登录") from None
    token = complete_login(user)
    # CAS 避免并发登录回退签名计数器，也拒绝验证期间被删除的凭据。
    updated = db.query(PasskeyModel).filter(
        PasskeyModel.id == passkey.id,
        PasskeyModel.user_id == user.id,
        PasskeyModel.credential_id == credential.id,
        PasskeyModel.public_key == passkey.public_key,
        PasskeyModel.sign_count == passkey.sign_count,
    ).update({
        PasskeyModel.sign_count: verified.new_sign_count,
        PasskeyModel.backed_up: verified.credential_backed_up,
        PasskeyModel.last_used_at: datetime.now(),
    }, synchronize_session=False)
    if updated != 1:
        db.rollback()
        raise HTTPException(status_code=401, detail="Passkey 状态已变化，请重新登录")
    db.commit()
    return token


@router.delete("/{passkey_id}")
def delete_passkey(passkey_id: int, body: PasswordConfirmation, request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    service.limit_request(request, "delete", limit=10)
    _confirm_password(user, body.password)
    passkey = db.query(PasskeyModel).filter(PasskeyModel.id == passkey_id, PasskeyModel.user_id == user.id).first()
    if passkey is None:
        raise HTTPException(status_code=404, detail="Passkey 不存在")
    db.delete(passkey)
    db.commit()
    return {"ok": True}
