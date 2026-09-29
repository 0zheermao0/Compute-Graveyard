from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import admin, auth
from app.auth import create_access_token
from app.config import MAX_GPUS_PER_USER
from app.database import Base, _migrate_gpu_quota, get_db
from app.database_models import UserModel


def test_gpu_quota_migration_backfills_missing_and_null_without_overwriting_values():
    engine = create_engine("sqlite:///:memory:")
    users = Table("users", MetaData(), Column("id", Integer, primary_key=True), Column("username", String(64)))
    users.create(engine)
    with engine.begin() as conn:
        conn.execute(users.insert().values(id=1, username="old"))
    _migrate_gpu_quota(engine)
    _migrate_gpu_quota(engine)
    assert "max_gpus_per_user" in {column["name"] for column in inspect(engine).get_columns("users")}
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET max_gpus_per_user = 0 WHERE id = 1"))
        conn.execute(text("INSERT INTO users (id, username) VALUES (2, 'new')"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT max_gpus_per_user FROM users ORDER BY id")).scalars().all() == [0, MAX_GPUS_PER_USER]
    engine.dispose()

    engine = create_engine("sqlite:///:memory:")
    users = Table("users", MetaData(), Column("id", Integer, primary_key=True), Column("max_gpus_per_user", Integer))
    users.create(engine)
    with engine.begin() as conn:
        conn.execute(users.insert(), [{"id": 1, "max_gpus_per_user": None}, {"id": 2, "max_gpus_per_user": 5}])
    _migrate_gpu_quota(engine)
    _migrate_gpu_quota(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT max_gpus_per_user FROM users ORDER BY id")).scalars().all() == [MAX_GPUS_PER_USER, 5]
    engine.dispose()


def test_gpu_quota_admin_api_and_new_user_defaults(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([UserModel(username="boss", hashed_password="x", role="admin", approved=1),
                UserModel(username="alice", hashed_password="x", role="user", approved=1)])
    db.commit()
    alice = db.query(UserModel).filter_by(username="alice").one()
    assert alice.max_gpus_per_user == MAX_GPUS_PER_USER
    app = FastAPI()
    app.include_router(admin.router, prefix="/api/admin")
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(admin, "get_password_hash", lambda _: "x")
    with TestClient(app) as client:
        path = f"/api/admin/users/{alice.id}/gpu-quota"
        headers = {"Authorization": f"Bearer {create_access_token({'sub': 'boss'})}"}
        non_admin = {"Authorization": f"Bearer {create_access_token({'sub': 'alice'})}"}
        assert client.get("/api/admin/users").status_code == 401
        assert client.put(path, json={"max_gpus_per_user": 1}, headers=non_admin).status_code == 403
        assert client.get("/api/admin/users", headers=headers).json()[1]["max_gpus_per_user"] == MAX_GPUS_PER_USER
        for value in (-1, 1.5, True, "3", None, 2**31):
            assert client.put(path, json={"max_gpus_per_user": value}, headers=headers).status_code == 400
        for body in ({}, {"quota": 2}, {"max_gpus_per_user": 2, "extra": 1}, []):
            assert client.put(path, json=body, headers=headers).status_code == 400
        assert client.put("/api/admin/users/999/gpu-quota", json={"max_gpus_per_user": 1}, headers=headers).status_code == 404
        assert client.put(path, json={"max_gpus_per_user": 0}, headers=headers).json() == {"id": alice.id, "max_gpus_per_user": 0}
        db.expire_all()
        assert db.get(UserModel, alice.id).max_gpus_per_user == 0
        assert client.get("/api/admin/users", headers=headers).json()[1]["max_gpus_per_user"] == 0
        assert client.post("/api/admin/users", json={"username": "newadminuser", "password": "password"}, headers=headers).status_code == 200
        assert db.query(UserModel).filter_by(username="newadminuser").one().max_gpus_per_user == MAX_GPUS_PER_USER
        assert client.post("/api/auth/register", json={"username": "newselfuser", "password": "password", "real_name": "Self", "contact_type": "wechat", "contact_value": "self"}).status_code == 200
        assert db.query(UserModel).filter_by(username="newselfuser").one().max_gpus_per_user == MAX_GPUS_PER_USER
    db.close()
    engine.dispose()
