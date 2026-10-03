"""同步数据库认证依赖不得在事件循环中等待连接池。"""
import asyncio
import hashlib
import time
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.api import passkeys, personal
from app.auth import create_access_token
from app.database import Base, _create_database_engine, get_db
from app.database_models import PersonalTokenModel, UserModel


@pytest.mark.parametrize("pool_kind, request_count", [
    ("production", 16), ("production", 32), ("production", 64),
    ("bounded", 16), ("bounded", 32),
])
@pytest.mark.parametrize("credential_kind", ["jwt", "personal"])
def test_authenticated_requests_finish_when_exceeding_connection_pool_capacity(tmp_path, pool_kind, request_count, credential_kind):
    url = f"sqlite:///{tmp_path / 'concurrent-auth.db'}"
    # 生产 SQLite 必须在超过默认 40 个工作线程的并发下仍能完成请求。
    # 另用最大 15 连接的 QueuePool 验证认证查询确实不在事件循环中执行。
    engine = _create_database_engine(url) if pool_kind == "production" else create_engine(
        url, connect_args={"check_same_thread": False}, poolclass=QueuePool, pool_timeout=0.5,
    )
    Base.metadata.create_all(engine)
    personal_token = "cgpat_test_concurrent_authentication"
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="unused", approved=1)
        db.add(user)
        db.flush()
        db.add(PersonalTokenModel(
            user_id=user.id, name="test", token_hash=hashlib.sha256(personal_token.encode()).hexdigest(),
            expires_at=datetime.now() + timedelta(days=1),
        ))
        db.commit()

    @event.listens_for(engine, "after_cursor_execute")
    def keep_connections_busy(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            time.sleep(0.01)

    app = FastAPI()
    app.include_router(passkeys.router, prefix="/api/auth/passkeys")

    @app.get("/personal-auth-check")
    def personal_auth_check(user=Depends(personal.get_personal_user)):
        return {"username": user.username}

    def isolated_session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = isolated_session
    token = personal_token if credential_kind == "personal" else create_access_token({"sub": "alice"})
    path = "/personal-auth-check" if credential_kind == "personal" else "/api/auth/passkeys"

    async def concurrent_requests():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await asyncio.gather(*[
                client.get(path, headers={"Authorization": f"Bearer {token}"})
                for _ in range(request_count)
            ])

    try:
        responses = asyncio.run(concurrent_requests())
        assert all(response.status_code == 200 for response in responses), [
            (response.status_code, response.text) for response in responses if response.status_code != 200
        ]
        expected = {"username": "alice"} if credential_kind == "personal" else []
        assert all(response.json() == expected for response in responses)
    finally:
        engine.dispose()
