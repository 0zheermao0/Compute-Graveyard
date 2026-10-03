"""WebAuthn 的受信任来源、一次性挑战及请求限流。"""
import json
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json

from app.config import WEBAUTHN_ORIGINS, WEBAUTHN_RP_ID, WEBAUTHN_RP_NAME as WEBAUTHN_RP_NAME
from app.database_models import PasskeyChallengeModel, UserModel

CHALLENGE_TTL_SECONDS = 300
MAX_PASSKEYS_PER_USER = 20
_limiter_lock = threading.Lock()


class _RateLimiter:
    """当前应用进程的有界限流器；不信任客户端传入的代理 IP 头。"""

    def __init__(self):
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def check(self, key, limit):
        now = time.monotonic()
        cutoff = now - 60
        with self.lock:
            while self.entries and next(iter(self.entries.values()))[-1] <= cutoff:
                self.entries.popitem(last=False)
            attempts = self.entries.get(key)
            if attempts is None:
                if len(self.entries) >= 4096:
                    raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试", headers={"Retry-After": "60"})
                attempts = deque()
                self.entries[key] = attempts
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= limit:
                raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试", headers={"Retry-After": "60"})
            attempts.append(now)
            self.entries.move_to_end(key)


def limit_request(request: Request, action: str, limit: int = 30):
    with _limiter_lock:
        if not hasattr(request.app.state, "passkey_rate_limiter"):
            request.app.state.passkey_rate_limiter = _RateLimiter()
    host = request.client.host if request.client else "unknown"
    request.app.state.passkey_rate_limiter.check((action, host), limit)


def request_origin(request: Request) -> str:
    """仅接受配置白名单中的浏览器来源，绝不使用 Host 推断 RP。"""
    origin = request.headers.get("origin", "")
    if origin not in WEBAUTHN_ORIGINS:
        raise HTTPException(status_code=403, detail="当前站点未配置为允许使用 Passkey 的来源")
    parsed = urlsplit(origin)
    hostname = parsed.hostname or ""
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not (parsed.scheme == "https" or (parsed.scheme == "http" and hostname == "localhost"))
    ):
        raise HTTPException(status_code=503, detail="Passkey 需要 HTTPS（localhost 开发环境除外），请检查 WEBAUTHN_ORIGINS")
    if not WEBAUTHN_RP_ID or not (hostname == WEBAUTHN_RP_ID or hostname.endswith("." + WEBAUTHN_RP_ID)):
        raise HTTPException(status_code=503, detail="Passkey 来源与 WEBAUTHN_RP_ID 不匹配，请联系管理员")
    return origin


def ensure_user_handle(db, user) -> bytes:
    if not user.webauthn_user_handle:
        # 条件更新确保两个同时发起的绑定流程使用同一个稳定、随机的 handle。
        db.query(UserModel).filter(
            UserModel.id == user.id, UserModel.webauthn_user_handle.is_(None),
        ).update({UserModel.webauthn_user_handle: secrets.token_urlsafe(32)}, synchronize_session=False)
        db.commit()
        db.refresh(user)
    return base64url_to_bytes(user.webauthn_user_handle)


def create_challenge(db, options, kind: str, origin: str, user_id=None) -> dict:
    now = datetime.now()
    db.query(PasskeyChallengeModel).filter(PasskeyChallengeModel.expires_at <= now).delete(synchronize_session=False)
    challenge_id = secrets.token_urlsafe(32)
    db.add(PasskeyChallengeModel(
        id=challenge_id,
        challenge=bytes_to_base64url(options.challenge),
        kind=kind,
        user_id=user_id,
        rp_id=WEBAUTHN_RP_ID,
        origin=origin,
        expires_at=now + timedelta(seconds=CHALLENGE_TTL_SECONDS),
    ))
    db.commit()
    return {"challenge_id": challenge_id, "options": json.loads(options_to_json(options))}


@dataclass(frozen=True)
class Ceremony:
    challenge: bytes
    rp_id: str
    origin: str


def consume_challenge(db, challenge_id: str, kind: str, origin: str, user_id=None) -> Ceremony:
    query = db.query(PasskeyChallengeModel).filter(
        PasskeyChallengeModel.id == challenge_id,
        PasskeyChallengeModel.kind == kind,
        PasskeyChallengeModel.user_id == user_id,
        PasskeyChallengeModel.rp_id == WEBAUTHN_RP_ID,
        PasskeyChallengeModel.origin == origin,
        PasskeyChallengeModel.expires_at > datetime.now(),
    )
    row = query.first()
    if row is None:
        raise HTTPException(status_code=400, detail="Passkey 验证已过期或已使用，请重新发起")
    ceremony = Ceremony(base64url_to_bytes(row.challenge), row.rp_id, row.origin)
    # SELECT 后使用条件 DELETE 的行数抢占。提交先于签名验证，失败的挑战也不可重用。
    if query.delete(synchronize_session=False) != 1:
        db.rollback()
        raise HTTPException(status_code=400, detail="Passkey 验证已过期或已使用，请重新发起")
    db.commit()
    return ceremony
