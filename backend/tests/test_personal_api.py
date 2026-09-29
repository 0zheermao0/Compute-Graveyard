import hashlib
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import admin, personal
from app.auth import create_access_token
from app.database import Base, get_db
from app.database_models import ContainerModel, PersonalTokenModel, UserModel


def test_personal_token_lifecycle_and_container_isolation(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        alice = UserModel(username="alice", hashed_password="x", approved=1)
        bob = UserModel(username="bob", hashed_password="x", approved=1)
        db.add_all([alice, bob])
        db.flush()
        now = datetime.now()
        db.add_all([
            ContainerModel(container_id="alice-running", name="alice-box", user_id=alice.id, node_id="node-1", node_name="node one", access_host="192.0.2.1", gpu_ids="0", ssh_port=2222, ssh_password="alice-password", extra_ports='{"8888": 30123}', status="running", expires_at=now + timedelta(days=1)),
            ContainerModel(container_id="bob-running", name="bob-box", user_id=bob.id, node_id="node-1", gpu_ids="1", ssh_port=2223, ssh_password="bob-password", status="running", expires_at=now + timedelta(days=1)),
            ContainerModel(container_id="old", name="old-box", user_id=alice.id, gpu_ids="1", ssh_port=2224, ssh_password="old-password", status="running", expires_at=now - timedelta(seconds=1)),
        ])
        db.commit()
        monkeypatch.setattr(personal, "aggregate_inventories", lambda _: [{"node": {"id": "node-1"}, "online": True, "inventory": {"gpus": [
            {"index": 0, "name": "A100", "utilization": 42, "memory_used_mb": 512, "memory_total_mb": 1024, "memory_percent": 50},
            {"index": 1, "name": "H100", "utilization": 80},
        ]}}])
        app = FastAPI()
        app.include_router(personal.router, prefix="/api/personal")
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            alice_jwt = {"Authorization": f"Bearer {create_access_token({'sub': 'alice'})}"}
            bob_jwt = {"Authorization": f"Bearer {create_access_token({'sub': 'bob'})}"}
            path = "/api/personal/tokens"
            assert client.post(path, json={"name": "agent"}).status_code == 401
            assert client.post(path, headers=alice_jwt, json={"name": "  "}).status_code == 422
            assert client.post(path, headers=alice_jwt, json={"name": "agent", "expires_in_days": 366}).status_code == 422
            created = client.post(path, headers=alice_jwt, json={"name": " agent ", "expires_in_days": 1})
            assert created.status_code == 201
            info = created.json()
            token = info.pop("token")
            assert info["name"] == "agent"
            assert db.query(PersonalTokenModel).one().token_hash == hashlib.sha256(token.encode()).hexdigest()
            assert token not in str(db.query(PersonalTokenModel).one().__dict__)
            bearer = {"Authorization": f"Bearer {token}"}
            assert client.get(path, headers=bearer).status_code == 401
            assert client.post(path, headers=bearer, json={"name": "bad"}).status_code == 401
            assert client.delete(f"{path}/{info['id']}", headers=bob_jwt).status_code == 404
            assert client.get(path, headers=alice_jwt).json() == [info]
            assert token not in str(client.get("/api/personal/docs", headers=alice_jwt).json())
            assert client.get("/api/personal/containers", headers=alice_jwt).status_code == 401
            response = client.get("/api/personal/containers", headers=bearer)
            assert response.status_code == 200
            assert len(response.json()) == 1
            container = response.json()[0]
            assert container["name"] == "alice-box"
            assert container["gpus"] == [{"index": 0, "name": "A100", "utilization": 42, "memory_used_mb": 512, "memory_total_mb": 1024, "memory_percent": 50.0, "temperature": None}]
            assert container["access_host"] == "192.0.2.1"
            assert container["ssh_password"] == "alice-password"
            assert container["extra_ports"] == {"8888": 30123}
            assert "bob" not in response.text and "old-password" not in response.text
            assert client.delete(f"{path}/{info['id']}", headers=alice_jwt).status_code == 204
            assert client.get("/api/personal/containers", headers=bearer).status_code == 401
            assert client.get(path, headers=alice_jwt).json()[0]["revoked_at"] is not None
    engine.dispose()


def test_expired_tokens_unapproved_users_and_offline_metrics(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="x", approved=1)
        db.add(user)
        db.flush()
        db.add(ContainerModel(container_id="c1", name="c1", user_id=user.id, gpu_ids="0", ssh_port=2222, status="running", expires_at=datetime.now() + timedelta(days=1)))
        db.commit()
        monkeypatch.setattr(personal, "aggregate_inventories", lambda _: [])
        app = FastAPI()
        app.include_router(personal.router, prefix="/api/personal")
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            jwt = {"Authorization": f"Bearer {create_access_token({'sub': 'alice'})}"}
            token = client.post("/api/personal/tokens", headers=jwt, json={"name": "test"}).json()["token"]
            bearer = {"Authorization": f"Bearer {token}"}
            assert client.get("/api/personal/containers", headers=bearer).json()[0]["gpus"] == []
            user.approved = 0
            db.commit()
            assert client.get("/api/personal/containers", headers=bearer).status_code == 401
            assert client.post("/api/personal/tokens", headers=jwt, json={"name": "test"}).status_code == 403
            user.approved = 1
            db.query(PersonalTokenModel).one().expires_at = datetime.now() - timedelta(seconds=1)
            db.commit()
            assert client.get("/api/personal/containers", headers=bearer).status_code == 401
    engine.dispose()


def test_user_deletion_cleans_tokens_with_foreign_keys():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="x", approved=1)
        admin_user = UserModel(username="boss", hashed_password="x", role="admin", approved=1)
        db.add_all([user, admin_user])
        db.flush()
        db.add(PersonalTokenModel(user_id=user.id, name="agent", token_hash="a" * 64, expires_at=datetime.now() + timedelta(days=1)))
        db.commit()
        assert admin.delete_user(user.id, admin=admin_user, db=db)["message"]
        assert db.query(PersonalTokenModel).count() == 0
    engine.dispose()
