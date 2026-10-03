"""Passkey API integration tests with real ES256 WebAuthn ceremonies.

The small software authenticator below produces browser-shaped JSON, a CBOR
``none`` attestation, and real cryptography signatures. No verifier is mocked,
and the application is assembled without importing main.py or its scheduler.
"""

import base64
import hashlib
import itertools
import json
import secrets
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Barrier, Event

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Text, create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers.exceptions import InvalidAuthenticationResponse

from app.api import auth, passkeys
from app.auth import create_access_token, decode_token, get_password_hash
from app.database import Base, _create_database_engine, _migrate_passkeys, get_db
from app.database_models import (
    PasskeyChallengeModel,
    PasskeyModel,
    PersonalTokenModel,
    UserModel,
)


PATH = "/api/auth/passkeys"
ORIGIN = "http://localhost:5173"
PASSWORD = "correct horse passkey battery"
UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40
METADATA_FIELDS = {"id", "name", "created_at", "last_used_at", "backed_up"}
_CLIENT_IDS = itertools.count(1)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _rp_id(options: dict) -> str:
    return options["rp"]["id"] if "rp" in options else options["rpId"]


def _client_data(options, client_type, origin, challenge, cross_origin):
    return json.dumps({
        "type": client_type,
        "challenge": options["challenge"] if challenge is None else challenge,
        "origin": origin,
        "crossOrigin": cross_origin,
    }, separators=(",", ":")).encode()


@dataclass
class SoftwareAuthenticator:
    credential_id: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    private_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    @property
    def public_key(self):
        numbers = self.private_key.public_key().public_numbers()
        # EC2 / ES256 / P-256, with the two affine coordinates.
        return cbor2.dumps({
            1: 2,
            3: -7,
            -1: 1,
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        })

    def registration(
        self, options, *, origin=ORIGIN, rp_id=None, flags=UP | UV | AT,
        sign_count=0, challenge=None, client_type="webauthn.create",
        cross_origin=False,
    ):
        client_data = _client_data(options, client_type, origin, challenge, cross_origin)
        auth_data = (
            hashlib.sha256((rp_id or _rp_id(options)).encode()).digest()
            + bytes([flags])
            + struct.pack(">I", sign_count)
            + bytes(16)  # AAGUID
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self.public_key
        )
        identifier = _b64(self.credential_id)
        return {
            "id": identifier,
            "rawId": identifier,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": _b64(client_data),
                "attestationObject": _b64(cbor2.dumps({
                    "fmt": "none", "attStmt": {}, "authData": auth_data,
                })),
                "transports": ["internal"],
            },
        }

    def authentication(
        self, options, *, user_handle, sign_count=1, origin=ORIGIN,
        rp_id=None, flags=UP | UV, challenge=None,
        client_type="webauthn.get", cross_origin=False, signing_key=None,
    ):
        client_data = _client_data(options, client_type, origin, challenge, cross_origin)
        auth_data = (
            hashlib.sha256((rp_id or _rp_id(options)).encode()).digest()
            + bytes([flags])
            + struct.pack(">I", sign_count)
        )
        signature = (signing_key or self.private_key).sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        identifier = _b64(self.credential_id)
        return {
            "id": identifier,
            "rawId": identifier,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": _b64(client_data),
                "authenticatorData": _b64(auth_data),
                "signature": _b64(signature),
                "userHandle": user_handle,
            },
        }


def _assert_rejected(response):
    assert 400 <= response.status_code < 500, response.text
    # A rate-limit rejection must not accidentally make a security test pass.
    assert response.status_code != 429, response.text
    assert "access_token" not in response.json()


def _engine(foreign_keys=False):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    if foreign_keys:
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
    return engine


@dataclass
class PasskeyAPI:
    client: TestClient
    db: Session
    users: dict

    def headers(self, username="alice"):
        return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}

    def register_options(self, username="alice"):
        response = self.client.post(
            f"{PATH}/register/options", headers=self.headers(username), json={"password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def login_options(self):
        response = self.client.post(f"{PATH}/login/options", json={})
        assert response.status_code == 200, response.text
        return response.json()

    def register(self, authenticator=None, username="alice", name="Laptop", **kwargs):
        authenticator = authenticator or SoftwareAuthenticator()
        issued = self.register_options(username)
        response = self.client.post(
            f"{PATH}/register/verify", headers=self.headers(username), json={
                "challenge_id": issued["challenge_id"],
                "credential": authenticator.registration(issued["options"], **kwargs),
                "name": name,
            },
        )
        assert response.status_code == 201, response.text
        return authenticator, issued, response.json()

    def verify_login(self, issued, credential):
        return self.client.post(f"{PATH}/login/verify", json={
            "challenge_id": issued["challenge_id"], "credential": credential,
        })


@pytest.fixture
def passkey_api(request):
    engine = _engine(foreign_keys=getattr(request, "param", False))
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            hashed_password = get_password_hash(PASSWORD)
            users = {
                username: UserModel(
                    username=username, hashed_password=hashed_password,
                    display_name=username.title(), approved=approved,
                )
                for username, approved in [("alice", 1), ("bob", 1), ("pending", 0)]
            }
            db.add_all(users.values())
            db.flush()
            # Model defaults may give new accounts a handle. Simulate existing
            # accounts whose nullable column was added by the legacy migration.
            for user in users.values():
                user.webauthn_user_handle = None
            db.commit()
            app = FastAPI()
            app.include_router(auth.router, prefix="/api/auth")
            app.include_router(passkeys.router, prefix=PATH)
            app.dependency_overrides[get_db] = lambda: db
            # Each test has its own client IP, without changing the real limiter.
            client_ip = f"192.0.2.{next(_CLIENT_IDS)}"

            @app.middleware("http")
            async def identify_test_client(request, call_next):
                request.scope["client"] = (client_ip, 50000)
                return await call_next(request)

            with TestClient(app, headers={"Origin": ORIGIN}) as client:
                yield PasskeyAPI(client, db, users)
    finally:
        engine.dispose()


def test_software_authenticator_uses_real_webauthn_verification():
    authenticator = SoftwareAuthenticator()
    options = {"challenge": _b64(secrets.token_bytes(32)), "rpId": "localhost"}
    registration = verify_registration_response(
        credential=authenticator.registration(options, sign_count=2),
        expected_challenge=_unb64(options["challenge"]), expected_rp_id="localhost",
        expected_origin=ORIGIN, require_user_verification=True,
    )
    assert registration.credential_id == authenticator.credential_id
    assert registration.credential_public_key == authenticator.public_key
    assert registration.sign_count == 2
    arguments = {
        "expected_challenge": _unb64(options["challenge"]),
        "expected_rp_id": "localhost", "expected_origin": ORIGIN,
        "credential_public_key": registration.credential_public_key,
        "credential_current_sign_count": 2, "require_user_verification": True,
    }
    credential = authenticator.authentication(options, user_handle=_b64(bytes(32)), sign_count=3)
    assert verify_authentication_response(credential=credential, **arguments).new_sign_count == 3
    credential = authenticator.authentication(
        options, user_handle=_b64(bytes(32)), sign_count=3,
        signing_key=ec.generate_private_key(ec.SECP256R1()),
    )
    with pytest.raises(InvalidAuthenticationResponse):
        verify_authentication_response(credential=credential, **arguments)


def test_registration_login_and_list_metadata_match_existing_login(passkey_api):
    api = passkey_api
    assert api.users["alice"].webauthn_user_handle is None
    assert api.client.get(PATH, headers=api.headers()).json() == []
    authenticator, issued, metadata = api.register()
    options = issued["options"]
    handle = options["user"]["id"]
    assert len(_unb64(handle)) == 32
    assert _b64(_unb64(handle)) == handle
    assert api.users["alice"].webauthn_user_handle == handle
    assert options["user"]["name"] == "alice"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert set(metadata) == METADATA_FIELDS
    assert metadata["name"] == "Laptop"
    assert metadata["created_at"] and metadata["last_used_at"] is None
    assert metadata["backed_up"] is False
    row = api.db.query(PasskeyModel).one()
    assert row.user_id == api.users["alice"].id
    assert row.credential_id == _b64(authenticator.credential_id)
    assert row.public_key == _b64(authenticator.public_key)
    assert row.rp_id == _rp_id(options)
    assert row.sign_count == 0
    assert json.loads(row.transports) == ["internal"]
    assert api.db.get(PasskeyChallengeModel, issued["challenge_id"]) is None
    listed = api.client.get(PATH, headers=api.headers())
    assert listed.status_code == 200
    assert listed.json() == [metadata]
    assert row.public_key not in listed.text and row.credential_id not in listed.text
    assert api.client.get(PATH, headers=api.headers("bob")).json() == []

    login = api.login_options()
    assert login["options"]["userVerification"] == "required"
    assert not login["options"].get("allowCredentials")  # Discoverable, username-free login.
    challenge = api.db.get(PasskeyChallengeModel, login["challenge_id"])
    assert challenge.kind == "login" and challenge.user_id is None
    assert challenge.rp_id == row.rp_id and challenge.origin == ORIGIN
    assert datetime.now() < challenge.expires_at <= datetime.now() + timedelta(minutes=5, seconds=5)
    credential = authenticator.authentication(login["options"], user_handle=handle)
    response = api.verify_login(login, credential)
    assert response.status_code == 200, response.text
    password_login = api.client.post(
        "/api/auth/login/json", json={"username": "alice", "password": PASSWORD}
    )
    assert password_login.status_code == 200
    token = response.json()
    assert set(token) == set(password_login.json())
    assert token["token_type"] == "bearer"
    assert token["user"] == password_login.json()["user"]
    assert decode_token(token["access_token"])["sub"] == "alice"
    me = api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token['access_token']}"})
    assert me.status_code == 200 and me.json() == token["user"]
    api.db.refresh(row)
    assert row.sign_count == 1 and row.last_used_at is not None
    assert api.db.get(PasskeyChallengeModel, login["challenge_id"]) is None
    latest = api.client.get(PATH, headers=api.headers()).json()[0]
    assert set(latest) == METADATA_FIELDS and latest["last_used_at"]
    _assert_rejected(api.verify_login(login, credential))
    _assert_rejected(api.client.post(
        f"{PATH}/register/verify", headers=api.headers(), json={
            "challenge_id": issued["challenge_id"],
            "credential": authenticator.registration(options), "name": "Replay",
        },
    ))
    assert api.db.query(PasskeyModel).count() == 1


def test_user_handle_is_stable_unique_and_existing_keys_are_excluded(passkey_api):
    api = passkey_api
    first, issued, _ = api.register()
    next_options = api.register_options()
    assert next_options["options"]["user"]["id"] == issued["options"]["user"]["id"]
    assert _b64(first.credential_id) in {
        entry["id"] for entry in next_options["options"]["excludeCredentials"]
    }
    registration_challenge = api.db.get(PasskeyChallengeModel, next_options["challenge_id"])
    assert registration_challenge.kind == "register"
    assert registration_challenge.user_id == api.users["alice"].id
    assert registration_challenge.origin == ORIGIN
    assert registration_challenge.rp_id == _rp_id(next_options["options"])
    assert datetime.now() < registration_challenge.expires_at <= datetime.now() + timedelta(minutes=5, seconds=5)
    _, second, _ = api.register(name="Phone", flags=UP | UV | AT | BE | BS)
    assert second["options"]["user"]["id"] == issued["options"]["user"]["id"]
    bob_options = api.register_options("bob")
    assert bob_options["options"]["user"]["id"] != issued["options"]["user"]["id"]
    assert len(_unb64(bob_options["options"]["user"]["id"])) == 32
    listed = api.client.get(PATH, headers=api.headers()).json()
    assert {item["name"] for item in listed} == {"Laptop", "Phone"}
    assert next(item for item in listed if item["name"] == "Phone")["backed_up"] is True
    assert all(set(item) == METADATA_FIELDS for item in listed)


@pytest.mark.parametrize("bearer", ["missing", "personal", "expired-jwt"])
def test_management_endpoints_require_a_current_jwt(passkey_api, bearer):
    api = passkey_api
    _, _, metadata = api.register()
    issued = api.register_options()
    credential = SoftwareAuthenticator().registration(issued["options"])
    if bearer == "personal":
        token = "cgpat_" + secrets.token_urlsafe(32)
        api.db.add(PersonalTokenModel(
            user_id=api.users["alice"].id, name="agent",
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=datetime.now() + timedelta(days=1),
        ))
        api.db.commit()
        headers = {"Authorization": f"Bearer {token}"}
    elif bearer == "expired-jwt":
        # A comfortably old timestamp also avoids naive local/UTC differences
        # in the existing JWT helper on developer machines.
        token = create_access_token({"sub": "alice"}, expires_delta=timedelta(days=-2))
        headers = {"Authorization": f"Bearer {token}"}
    else:
        headers = {}
    requests = [
        ("GET", PATH, None),
        ("POST", f"{PATH}/register/options", {"password": PASSWORD}),
        ("POST", f"{PATH}/register/verify", {
            "challenge_id": issued["challenge_id"], "credential": credential, "name": "Unauthorized",
        }),
        ("DELETE", f"{PATH}/{metadata['id']}", {"password": PASSWORD}),
    ]
    for method, path, body in requests:
        response = api.client.request(method, path, headers=headers, json=body)
        assert response.status_code == 401, response.text
    assert api.db.query(PasskeyModel).count() == 1


def test_registration_requires_password_confirmation(passkey_api):
    api = passkey_api
    for body in [{}, {"password": "wrong password"}]:
        _assert_rejected(api.client.post(f"{PATH}/register/options", headers=api.headers(), json=body))
    assert api.users["alice"].webauthn_user_handle is None
    assert api.db.query(PasskeyChallengeModel).count() == 0
    assert api.db.query(PasskeyModel).count() == 0
    assert api.client.post(
        f"{PATH}/register/options", headers=api.headers("pending"), json={"password": PASSWORD}
    ).status_code == 403


def test_registration_challenge_cannot_be_used_by_another_user(passkey_api):
    api = passkey_api
    issued = api.register_options()
    response = api.client.post(f"{PATH}/register/verify", headers=api.headers("bob"), json={
        "challenge_id": issued["challenge_id"],
        "credential": SoftwareAuthenticator().registration(issued["options"]), "name": "Stolen",
    })
    _assert_rejected(response)
    assert api.db.query(PasskeyModel).count() == 0


@pytest.mark.parametrize("username", ["alice", "bob"])
def test_duplicate_credential_cannot_be_bound_twice_or_to_another_user(passkey_api, username):
    api = passkey_api
    authenticator, _, _ = api.register()
    issued = api.register_options(username)
    response = api.client.post(f"{PATH}/register/verify", headers=api.headers(username), json={
        "challenge_id": issued["challenge_id"],
        "credential": authenticator.registration(issued["options"]), "name": "Duplicate",
    })
    _assert_rejected(response)
    row = api.db.query(PasskeyModel).one()
    assert row.user_id == api.users["alice"].id and row.name == "Laptop"


@pytest.mark.parametrize("kind", ["register", "login"])
def test_expired_challenges_cannot_be_verified(passkey_api, kind):
    api = passkey_api
    if kind == "register":
        issued = api.register_options()
        credential = SoftwareAuthenticator().registration(issued["options"])
    else:
        authenticator, registration, _ = api.register()
        issued = api.login_options()
        credential = authenticator.authentication(
            issued["options"], user_handle=registration["options"]["user"]["id"]
        )
    challenge = api.db.get(PasskeyChallengeModel, issued["challenge_id"])
    challenge.expires_at = datetime.now() - timedelta(seconds=1)
    api.db.commit()
    if kind == "register":
        response = api.client.post(f"{PATH}/register/verify", headers=api.headers(), json={
            "challenge_id": issued["challenge_id"], "credential": credential, "name": "Expired",
        })
        assert api.db.query(PasskeyModel).count() == 0
    else:
        response = api.verify_login(issued, credential)
        assert api.db.query(PasskeyModel).one().sign_count == 0
    _assert_rejected(response)


def test_register_and_login_challenge_types_are_isolated(passkey_api):
    api = passkey_api
    authenticator, registered, _ = api.register()
    register = api.register_options()
    login = api.login_options()
    _assert_rejected(api.verify_login(register, authenticator.authentication(
        register["options"], user_handle=registered["options"]["user"]["id"]
    )))
    _assert_rejected(api.client.post(f"{PATH}/register/verify", headers=api.headers(), json={
        "challenge_id": login["challenge_id"],
        "credential": SoftwareAuthenticator().registration(login["options"]), "name": "Wrong type",
    }))
    row = api.db.query(PasskeyModel).one()
    assert row.sign_count == 0 and row.last_used_at is None


@pytest.mark.parametrize("invalid", [
    "origin", "allowed-origin-mismatch", "rp-id", "no-uv", "no-up",
    "challenge", "client-type", "cross-origin", "raw-id",
])
def test_registration_rejects_invalid_webauthn_data(passkey_api, invalid):
    api = passkey_api
    issued = api.register_options()
    arguments = {
        "origin": {"origin": "https://attacker.example"},
        "allowed-origin-mismatch": {"origin": "http://localhost:3000"},
        "rp-id": {"rp_id": "attacker.example"},
        "no-uv": {"flags": UP | AT},
        "no-up": {"flags": UV | AT},
        "challenge": {"challenge": _b64(secrets.token_bytes(32))},
        "client-type": {"client_type": "webauthn.get"},
        "cross-origin": {"cross_origin": True},
        "raw-id": {},
    }[invalid]
    credential = SoftwareAuthenticator().registration(issued["options"], **arguments)
    if invalid == "raw-id":
        credential["rawId"] = _b64(secrets.token_bytes(32))
    response = api.client.post(f"{PATH}/register/verify", headers=api.headers(), json={
        "challenge_id": issued["challenge_id"], "credential": credential, "name": "Invalid",
    })
    _assert_rejected(response)
    assert api.db.query(PasskeyModel).count() == 0


@pytest.mark.parametrize("invalid", [
    "origin", "allowed-origin-mismatch", "rp-id", "no-uv", "no-up",
    "challenge", "client-type", "cross-origin", "signature", "handle", "unknown-credential",
])
def test_login_rejects_invalid_signed_webauthn_data(passkey_api, invalid):
    api = passkey_api
    authenticator, registration, _ = api.register()
    handle = registration["options"]["user"]["id"]
    issued = api.login_options()
    arguments = {
        "origin": {"origin": "https://attacker.example"},
        "allowed-origin-mismatch": {"origin": "http://localhost:3000"},
        "rp-id": {"rp_id": "attacker.example"},
        "no-uv": {"flags": UP},
        "no-up": {"flags": UV},
        "challenge": {"challenge": _b64(secrets.token_bytes(32))},
        "client-type": {"client_type": "webauthn.create"},
        "cross-origin": {"cross_origin": True},
        "signature": {"signing_key": ec.generate_private_key(ec.SECP256R1())},
        "handle": {},
        "unknown-credential": {},
    }[invalid]
    signing_authenticator = SoftwareAuthenticator() if invalid == "unknown-credential" else authenticator
    credential = signing_authenticator.authentication(
        issued["options"], user_handle=_b64(secrets.token_bytes(32)) if invalid == "handle" else handle,
        **arguments,
    )
    _assert_rejected(api.verify_login(issued, credential))
    row = api.db.query(PasskeyModel).one()
    assert row.sign_count == 0 and row.last_used_at is None
    # A failed assertion must not corrupt the stored key or block a fresh ceremony.
    fresh = api.login_options()
    response = api.verify_login(fresh, authenticator.authentication(fresh["options"], user_handle=handle))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("kind", ["register", "login"])
def test_options_reject_disallowed_request_origin(passkey_api, kind):
    headers = {"Origin": "https://attacker.example"}
    body = {}
    if kind == "register":
        headers.update(passkey_api.headers())
        body["password"] = PASSWORD
    response = passkey_api.client.post(f"{PATH}/{kind}/options", headers=headers, json=body)
    _assert_rejected(response)
    assert passkey_api.db.query(PasskeyChallengeModel).count() == 0


@pytest.mark.parametrize("repeated_count", [0, 3])
def test_non_increasing_counters_are_rejected_and_large_counters_are_stored(passkey_api, repeated_count):
    api = passkey_api
    authenticator, registration, _ = api.register()
    handle = registration["options"]["user"]["id"]
    issued = api.login_options()
    response = api.verify_login(issued, authenticator.authentication(
        issued["options"], user_handle=handle, sign_count=3
    ))
    assert response.status_code == 200, response.text
    previous_used_at = api.db.query(PasskeyModel).one().last_used_at
    issued = api.login_options()
    _assert_rejected(api.verify_login(issued, authenticator.authentication(
        issued["options"], user_handle=handle, sign_count=repeated_count
    )))
    row = api.db.query(PasskeyModel).one()
    assert row.sign_count == 3 and row.last_used_at == previous_used_at
    issued = api.login_options()
    large_count = 2**31 + 7
    response = api.verify_login(issued, authenticator.authentication(
        issued["options"], user_handle=handle, sign_count=large_count
    ))
    assert response.status_code == 200, response.text
    api.db.refresh(row)
    assert row.sign_count == large_count


def test_authenticators_without_counters_can_login_and_backup_state_updates(passkey_api):
    api = passkey_api
    authenticator, registration, _ = api.register(flags=UP | UV | AT | BE)
    handle = registration["options"]["user"]["id"]
    for flags in [UP | UV | BE, UP | UV | BE | BS]:
        issued = api.login_options()
        response = api.verify_login(issued, authenticator.authentication(
            issued["options"], user_handle=handle, sign_count=0, flags=flags
        ))
        assert response.status_code == 200, response.text
        row = api.db.query(PasskeyModel).one()
        assert row.sign_count == 0
        assert row.backed_up is bool(flags & BS)
    assert api.client.get(PATH, headers=api.headers()).json()[0]["backed_up"] is True


@pytest.mark.parametrize("kind", ["register", "login"])
def test_user_must_still_be_approved_when_challenge_is_verified(passkey_api, kind):
    api = passkey_api
    if kind == "register":
        issued = api.register_options()
        credential = SoftwareAuthenticator().registration(issued["options"])
    else:
        authenticator, registration, _ = api.register()
        issued = api.login_options()
        credential = authenticator.authentication(
            issued["options"], user_handle=registration["options"]["user"]["id"]
        )
    api.users["alice"].approved = 0
    api.db.commit()
    if kind == "register":
        response = api.client.post(f"{PATH}/register/verify", headers=api.headers(), json={
            "challenge_id": issued["challenge_id"], "credential": credential, "name": "Unapproved",
        })
        assert api.db.query(PasskeyModel).count() == 0
    else:
        response = api.verify_login(issued, credential)
        assert api.db.query(PasskeyModel).one().sign_count == 0
    assert response.status_code == 403, response.text
    assert "access_token" not in response.json()


def test_deletion_is_user_scoped_and_requires_password_confirmation(passkey_api):
    api = passkey_api
    authenticator, registration, metadata = api.register()
    target = f"{PATH}/{metadata['id']}"
    response = api.client.request("DELETE", target, headers=api.headers("bob"), json={"password": PASSWORD})
    assert response.status_code == 404, response.text
    for body in [{}, {"password": "wrong password"}]:
        _assert_rejected(api.client.request("DELETE", target, headers=api.headers(), json=body))
        assert api.db.query(PasskeyModel).count() == 1
    response = api.client.request("DELETE", target, headers=api.headers(), json={"password": PASSWORD})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert api.db.query(PasskeyModel).count() == 0
    assert api.client.get(PATH, headers=api.headers()).json() == []
    issued = api.login_options()
    _assert_rejected(api.verify_login(issued, authenticator.authentication(
        issued["options"], user_handle=registration["options"]["user"]["id"]
    )))


@pytest.mark.parametrize("passkey_api", [False, True], indirect=True, ids=["orm-cascade", "foreign-keys"])
def test_user_deletion_cascades_keys_and_owned_challenges_and_prevents_login(passkey_api):
    api = passkey_api
    authenticator, registration, _ = api.register()
    api.register(username="bob", name="Bob laptop")
    owned = api.register_options()
    unrelated = api.register_options("bob")
    public = api.login_options()
    credential = authenticator.authentication(
        public["options"], user_handle=registration["options"]["user"]["id"]
    )
    alice_id = api.users["alice"].id
    api.db.delete(api.users["alice"])
    api.db.commit()
    assert api.db.get(UserModel, alice_id) is None
    remaining_key = api.db.query(PasskeyModel).one()
    assert remaining_key.user_id == api.users["bob"].id and remaining_key.name == "Bob laptop"
    assert api.db.get(PasskeyChallengeModel, owned["challenge_id"]) is None
    assert api.db.query(PasskeyChallengeModel).filter_by(user_id=alice_id).count() == 0
    assert api.db.get(PasskeyChallengeModel, unrelated["challenge_id"]) is not None
    assert api.db.get(PasskeyChallengeModel, public["challenge_id"]) is not None
    _assert_rejected(api.verify_login(public, credential))
    assert api.client.get(PATH, headers=api.headers()).status_code == 401


def test_passkey_models_use_text_keys_bigint_counters_and_nullable_handles():
    assert UserModel.__table__.columns.webauthn_user_handle.nullable is True
    columns = PasskeyModel.__table__.columns
    assert isinstance(columns.credential_id.type, Text)
    assert isinstance(columns.public_key.type, Text)
    assert isinstance(columns.transports.type, Text)
    assert isinstance(columns.sign_count.type, BigInteger)
    assert columns.user_id.nullable is False
    assert PasskeyChallengeModel.__table__.columns.user_id.nullable is True


def test_passkey_migration_preserves_legacy_users_is_idempotent_and_enforces_unique_handles(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.database.engine", engine)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, "
                "hashed_password TEXT NOT NULL)"
            ))
            connection.execute(text(
                "INSERT INTO users (id, username, hashed_password) VALUES "
                "(1, 'alice', 'legacy-hash'), (2, 'bob', 'another-hash')"
            ))
        _migrate_passkeys()  # The default bind must use the current database engine.
        _migrate_passkeys(engine)
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("users")}
        assert columns["webauthn_user_handle"]["nullable"] is True
        assert {PasskeyModel.__tablename__, PasskeyChallengeModel.__tablename__}.issubset(
            inspector.get_table_names()
        )
        assert any(
            index["unique"] and index["column_names"] == ["webauthn_user_handle"]
            for index in inspector.get_indexes("users")
        )
        with engine.begin() as connection:
            assert connection.execute(text(
                "SELECT id, username, hashed_password, webauthn_user_handle FROM users ORDER BY id"
            )).all() == [(1, "alice", "legacy-hash", None), (2, "bob", "another-hash", None)]
            handle = _b64(secrets.token_bytes(32))
            connection.execute(text(
                "UPDATE users SET webauthn_user_handle = :handle WHERE id = 1"
            ), {"handle": handle})
            # The unique index must allow several users that have not bound a key yet.
            connection.execute(text(
                "INSERT INTO users (id, username, hashed_password) VALUES (3, 'carol', 'third-hash')"
            ))
            authenticator = SoftwareAuthenticator()
            connection.execute(PasskeyModel.__table__.insert().values(
                user_id=1, credential_id=_b64(authenticator.credential_id),
                public_key=_b64(authenticator.public_key), sign_count=17,
                rp_id="localhost", name="Existing key", transports='["internal"]',
                backed_up=False, device_type="single_device",
            ))
            connection.execute(PasskeyChallengeModel.__table__.insert().values(
                id="existing-challenge", challenge=_b64(secrets.token_bytes(32)),
                kind="register", user_id=1, rp_id="localhost", origin=ORIGIN,
                expires_at=datetime.now() + timedelta(minutes=5),
            ))
        _migrate_passkeys(engine)
        _migrate_passkeys(engine)
        with engine.connect() as connection:
            existing_key = connection.execute(PasskeyModel.__table__.select()).mappings().one()
            assert existing_key["credential_id"] == _b64(authenticator.credential_id)
            assert existing_key["public_key"] == _b64(authenticator.public_key)
            assert existing_key["sign_count"] == 17 and existing_key["name"] == "Existing key"
            challenge = connection.execute(PasskeyChallengeModel.__table__.select()).mappings().one()
            assert challenge["id"] == "existing-challenge" and challenge["user_id"] == 1
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(
                    "UPDATE users SET webauthn_user_handle = :handle WHERE id = 2"
                ), {"handle": handle})
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT webauthn_user_handle FROM users ORDER BY id"
            )).scalars().all() == [handle, None, None]
            assert connection.execute(text("SELECT hashed_password FROM users WHERE id = 1")).scalar() == "legacy-hash"
    finally:
        engine.dispose()


def test_failed_assertion_consumes_its_challenge(passkey_api):
    api = passkey_api
    authenticator, registration, _ = api.register()
    issued = api.login_options()
    handle = registration["options"]["user"]["id"]
    wrong = authenticator.authentication(issued["options"], user_handle=handle, origin="https://attacker.example")
    assert api.verify_login(issued, wrong).status_code == 401
    valid = authenticator.authentication(issued["options"], user_handle=handle)
    assert api.verify_login(issued, valid).status_code == 400
    assert api.db.query(PasskeyModel).one().last_used_at is None


def test_public_options_are_rate_limited_without_writing_more_challenges(passkey_api):
    api = passkey_api
    for _ in range(30):
        api.login_options()
    rejected = api.client.post(f"{PATH}/login/options", json={})
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "60"
    assert api.db.query(PasskeyChallengeModel).count() == 30


def test_missing_origin_is_rejected_and_expired_challenges_are_cleaned(passkey_api):
    api = passkey_api
    assert api.client.post(f"{PATH}/login/options", headers={"Origin": ""}, json={}).status_code == 403
    issued = api.login_options()
    challenge = api.db.get(PasskeyChallengeModel, issued["challenge_id"])
    challenge.expires_at = datetime.now() - timedelta(seconds=1)
    api.db.commit()
    fresh = api.login_options()
    assert api.db.get(PasskeyChallengeModel, issued["challenge_id"]) is None
    assert api.db.get(PasskeyChallengeModel, fresh["challenge_id"]) is not None


def test_challenge_is_consumed_only_once_under_concurrent_requests(tmp_path):
    from app.webauthn_service import consume_challenge

    engine = create_engine(f"sqlite:///{tmp_path / 'passkey-race.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(PasskeyChallengeModel(
            id="race-challenge", challenge=_b64(secrets.token_bytes(32)), kind="login",
            rp_id="localhost", origin=ORIGIN, expires_at=datetime.now() + timedelta(minutes=5),
        ))
        db.commit()
    barrier = Barrier(2)

    @event.listens_for(engine, "after_cursor_execute")
    def synchronize_reads(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "passkey_challenges" in statement:
            barrier.wait(timeout=10)

    def attempt():
        with Session(engine) as db:
            try:
                consume_challenge(db, "race-challenge", "login", ORIGIN)
                return 200
            except HTTPException as exc:
                return exc.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        assert sorted(results) == [200, 400]
    finally:
        engine.dispose()


def test_concurrent_counter_update_cannot_be_rolled_back_by_login(passkey_api, monkeypatch):
    api = passkey_api
    authenticator, registration, _ = api.register()
    issued = api.login_options()
    original_verify = passkeys.verify_authentication_response
    credential_id = _b64(authenticator.credential_id)

    def verify_then_concurrent_update(**kwargs):
        result = original_verify(**kwargs)
        with Session(api.db.get_bind()) as other:
            other.query(PasskeyModel).filter_by(credential_id=credential_id).update({"sign_count": 2})
            other.commit()
        return result

    monkeypatch.setattr(passkeys, "verify_authentication_response", verify_then_concurrent_update)
    response = api.verify_login(issued, authenticator.authentication(
        issued["options"], user_handle=registration["options"]["user"]["id"], sign_count=1,
    ))
    assert response.status_code == 401
    api.db.expire_all()
    assert api.db.query(PasskeyModel).one().sign_count == 2


@pytest.mark.parametrize("replacement", ["owner", "public-key"])
def test_concurrent_credential_replacement_cannot_authenticate_old_owner(passkey_api, monkeypatch, replacement):
    api = passkey_api
    authenticator, registration, _ = api.register()
    issued = api.login_options()
    original_verify = passkeys.verify_authentication_response
    credential_id = _b64(authenticator.credential_id)
    changed = {"user_id": api.users["bob"].id} if replacement == "owner" else {
        "public_key": _b64(SoftwareAuthenticator().public_key),
    }

    def verify_then_replace_credential(**kwargs):
        result = original_verify(**kwargs)
        with Session(api.db.get_bind()) as other:
            other.query(PasskeyModel).filter_by(credential_id=credential_id).update(changed)
            other.commit()
        return result

    monkeypatch.setattr(passkeys, "verify_authentication_response", verify_then_replace_credential)
    response = api.verify_login(issued, authenticator.authentication(
        issued["options"], user_handle=registration["options"]["user"]["id"], sign_count=1,
    ))
    assert response.status_code == 401
    assert "access_token" not in response.json()
    api.db.expire_all()
    assert api.db.query(PasskeyModel).one().sign_count == 0


def test_failed_concurrent_session_cannot_resurrect_consumed_challenge(tmp_path):
    from app.webauthn_service import consume_challenge

    engine = _create_database_engine(f"sqlite:///{tmp_path / 'isolated-transactions.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(PasskeyChallengeModel(
            id="isolated-challenge", challenge=_b64(secrets.token_bytes(32)), kind="login",
            rp_id="localhost", origin=ORIGIN, expires_at=datetime.now() + timedelta(minutes=5),
        ))
        db.commit()
    deleted = Event()
    allow_commit = Event()

    @event.listens_for(engine, "after_cursor_execute")
    def pause_between_delete_and_commit(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("DELETE FROM PASSKEY_CHALLENGES"):
            deleted.set()
            assert allow_commit.wait(timeout=10)

    def consume():
        with Session(engine) as db:
            return consume_challenge(db, "isolated-challenge", "login", ORIGIN)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            success = executor.submit(consume)
            try:
                assert deleted.wait(timeout=10)
                # A failed request's dependency closes (rolls back) its Session
                # while the successful DELETE has not yet committed.
                with Session(engine) as other:
                    with pytest.raises(HTTPException) as failure:
                        consume_challenge(other, "missing-challenge", "login", ORIGIN)
                    assert failure.value.status_code == 400
            finally:
                allow_commit.set()
            assert success.result(timeout=10).origin == ORIGIN
        with Session(engine) as db:
            assert db.get(PasskeyChallengeModel, "isolated-challenge") is None
            with pytest.raises(HTTPException) as replay:
                consume_challenge(db, "isolated-challenge", "login", ORIGIN)
            assert replay.value.status_code == 400
    finally:
        allow_commit.set()
        engine.dispose()


def test_concurrent_registration_respects_per_user_capacity(tmp_path, monkeypatch):
    from fastapi import Request

    engine = _create_database_engine(f"sqlite:///{tmp_path / 'capacity-race.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()

    def browser_request():
        return Request({
            "type": "http", "app": app, "client": ("192.0.2.250", 50000),
            "headers": [(b"origin", ORIGIN.encode())],
        })

    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password=get_password_hash(PASSWORD), approved=1)
        db.add(user)
        db.flush()
        user_id = user.id
        template = SoftwareAuthenticator()
        db.add_all([
            PasskeyModel(
                user_id=user_id, credential_id=_b64(secrets.token_bytes(32)), public_key=_b64(template.public_key),
                sign_count=0, rp_id="localhost", name=f"Existing {index}", transports="[]", device_type="single_device",
            ) for index in range(19)
        ])
        db.commit()
        options = [passkeys.registration_options(
            passkeys.PasswordConfirmation(password=PASSWORD), browser_request(), user=user, db=db,
        ) for _ in range(2)]
    barrier = Barrier(2)
    original_verify = passkeys.verify_registration_response

    def synchronize_valid_responses(**kwargs):
        result = original_verify(**kwargs)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(passkeys, "verify_registration_response", synchronize_valid_responses)

    def register(issued):
        credential = SoftwareAuthenticator().registration(issued["options"])
        body = passkeys.RegistrationVerification(challenge_id=issued["challenge_id"], credential=credential, name="New key")
        with Session(engine) as db:
            user = db.get(UserModel, user_id)
            try:
                passkeys.registration_verify(body, browser_request(), user=user, db=db)
                return 201
            except HTTPException as exc:
                return exc.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(register, options))
        assert sorted(results) == [201, 400]
        with Session(engine) as db:
            assert db.query(PasskeyModel).filter_by(user_id=user_id).count() == 20
    finally:
        engine.dispose()
