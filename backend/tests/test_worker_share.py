import os
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import agent, containers
from app.auth import create_access_token
from app.database import Base, get_db
from app.database_models import ComputeNodeModel, ContainerModel, ShareRequestModel, UserModel, UserNotificationModel
from app import docker_service, quota_service, scheduler, worker_share


@pytest.fixture
def worker(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_share, "USER_DATA_BASE", tmp_path)
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([UserModel(id=1, username="owner", hashed_password="x"),
                UserModel(id=2, username="stranger", hashed_password="x")])
    db.commit()
    db.add(ContainerModel(container_id="owner-id", name="owner-container", node_id=agent.NODE_ID,
                          user_id=1, gpu_ids="0", status="running", ssh_port=22000,
                          expires_at=datetime.now() + timedelta(days=2)))
    db.commit()
    runtime = [{"container_id": "owner-id", "name": "owner-container", "username": "owner",
                "status": "running", "gpu_ids": "0"}]
    monkeypatch.setattr(agent, "NODE_ROLE", "worker")
    monkeypatch.setattr(agent, "AGENT_API_TOKEN", "secret")
    monkeypatch.setattr(containers, "NODE_ROLE", "worker")
    monkeypatch.setattr("app.worker_share.list_managed_containers", lambda **_: runtime)
    monkeypatch.setattr("app.worker_share.get_gpu_info", lambda: [{"index": 0}, {"index": 1}])
    monkeypatch.setattr("app.worker_share.get_setting", lambda *_: "2")
    monkeypatch.setattr(agent, "list_managed_containers", lambda: runtime)
    monkeypatch.setattr(agent, "allocate_service_ports", lambda: {8888: 30001})
    def inspect(container_id):
        item = next(item for item in runtime if item["container_id"] == container_id)
        return SimpleNamespace(name=item["name"], attrs={"Config": {
            "Labels": {"compute-graveyard.managed": "true", "compute-graveyard.request_id": item.get("request_id"),
                       "compute-graveyard.username": item["username"], "compute-graveyard.gpu_ids": item["gpu_ids"]},
            "Env": ["SSH_PASSWORD=" + item.get("ssh_password", "")]}})
    monkeypatch.setattr(worker_share, "get_docker_client", lambda: SimpleNamespace(containers=SimpleNamespace(get=inspect)))
    app = FastAPI()
    app.include_router(agent.router, prefix="/api/agent/v1")
    app.include_router(containers.router, prefix="/api/containers")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client, db, runtime, monkeypatch
    db.close()
    engine.dispose()


def request(client, **changes):
    body = {"request_id": "a" * 32, "applicant": "applicant", "username": "applicant",
            "name": "new-container", "gpu_ids": [0], "lease_days": 3, "mem_limit_gb": 8}
    body.update(changes)
    return client.post("/api/agent/v1/share-requests", json=body,
                       headers={"Authorization": "Bearer secret"})


def user_headers(username):
    return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}


@pytest.fixture
def master_share(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(UserModel(id=1, username="applicant", hashed_password="x", approved=1))
    db.add(ComputeNodeModel(id="worker-1", name="Worker", base_url="http://worker.example",
                            public_host="worker.example", agent_token="secret"))
    db.commit()
    inventory = {"node_id": "worker-1", "gpus": [{"index": 0}], "containers": [
        {"container_id": "owner-id", "name": "owner-container", "username": "owner",
         "status": "running", "gpu_ids": "0"}], "owners": [
        {"container_id": "owner-id", "container_name": "owner-container", "username": "owner",
         "gpu_ids": "0", "expires_at": (datetime.now() + timedelta(days=1)).isoformat()}],
        "system_load": {}}
    monkeypatch.setattr(containers, "NODE_ROLE", "master")
    monkeypatch.setattr("app.node_service.inventory_for_node", lambda *_: inventory)
    monkeypatch.setattr(containers, "inventory_for_node", lambda *_: inventory)
    monkeypatch.setattr(containers, "check_user_can_provision", lambda *_args, **_kw: type("Quota", (), {"allowed": True})())
    monkeypatch.setattr(containers, "get_setting", lambda *_: "8")
    state = {"state": "pending", "calls": 0, "cancelled": False}

    class Agent:
        def __init__(self, *_):
            pass

        def request_share(self, body):
            state["id"] = body["request_id"]
            state["body"] = body
            return self.share_status(state["id"])

        def share_status(self, request_id):
            return {"request_id": request_id, "state": state["state"],
                    "occupancy": [{"container_id": "owner-id", "name": "owner-container",
                                   "username": "owner", "gpu_ids": [0]}],
                    "approvers": [{"container_ids": ["owner-id"], "approved": state["state"] == "approved"}]}

        def recover_share(self, request_id):
            return self.share_status(request_id)

        def provision_share(self, request_id):
            state["calls"] += 1
            return {**self.share_status(request_id), "state": "provisioned", "ssh_password": "password",
                    "provision_result": {"container_id": "created-id", "ssh_port": 22001,
                                         "extra_ports": {}, "public_host": "worker.example"}}

        def cancel_share(self, request_id):
            state["cancelled"] = True
            return {"request_id": request_id, "state": "cancelled"}

    monkeypatch.setattr(containers, "RemoteAgentClient", Agent)
    app = FastAPI()
    app.include_router(containers.router, prefix="/api/containers")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client, db, inventory, state
    db.close()
    engine.dispose()


def test_master_remote_share_approval_and_idempotent_provision(master_share):
    client, db, _, state = master_share
    headers = user_headers("applicant")
    response = client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["pending_share_approval"]
    row = db.query(ContainerModel).first()
    assert json.loads(row.pending_share_json)["request_id"] == state["id"]
    assert state["body"]["applicant"] == "applicant"
    assert state["body"]["username"] == "applicant"
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "pending_share_approval"
    state["state"] = "approved"
    first = client.get("/api/containers/my", headers=headers).json()[0]
    assert first["status"] == "running"
    assert first["ssh_password"] == "password"
    assert client.get("/api/containers/my", headers=headers).json()[0]["container_id"] == "created-id"
    assert state["calls"] == 1


@pytest.mark.parametrize("mode", ["unknown", "mixed", "merge-old"])
def test_master_remote_share_rejects_unverified_occupancy(master_share, mode):
    client, db, inventory, state = master_share
    if mode == "unknown":
        inventory["owners"] = []
    elif mode == "mixed":
        db.add(ContainerModel(container_id="master-id", name="master", user_id=1, node_id="worker-1",
            gpu_ids="0", status="running", ssh_port=22002, expires_at=datetime.now() + timedelta(days=1)))
        db.commit()
    else:
        inventory["containers"][0]["name"] += "-merge-old"
    response = client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=user_headers("applicant"))
    assert response.status_code == 400
    assert "id" not in state
    assert db.query(ContainerModel).filter(ContainerModel.status == "pending_share_approval").count() == 0


def test_master_remote_share_cancellation_and_rejection(master_share):
    client, db, _, state = master_share
    headers = user_headers("applicant")
    body = {"placement_mode": "specific", "node_id": "worker-1", "gpu_ids": [0], "lease_days": 3}
    assert client.post("/api/containers/apply", json=body, headers=headers).status_code == 200
    row = db.query(ContainerModel).first()
    assert client.delete(f"/api/containers/{row.id}", headers=headers).status_code == 200
    assert state["cancelled"]
    assert row.status == "removed"
    assert client.post("/api/containers/apply", json=body, headers=headers).status_code == 200
    state["state"] = "rejected"
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "share_rejected"
    assert state["calls"] == 0


def test_master_remote_share_fails_closed_on_status_and_cancel_error(master_share, monkeypatch):
    client, db, _, state = master_share
    headers = user_headers("applicant")
    body = {"placement_mode": "specific", "node_id": "worker-1", "gpu_ids": [0], "lease_days": 3}
    assert client.post("/api/containers/apply", json=body, headers=headers).status_code == 200
    row = db.query(ContainerModel).first()
    from app.remote_agent import RemoteAgentError

    def unavailable(*_):
        raise RemoteAgentError("offline")

    monkeypatch.setattr(containers.RemoteAgentClient, "share_status", unavailable)
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "pending_share_approval"
    monkeypatch.setattr(containers.RemoteAgentClient, "cancel_share", unavailable)
    assert client.delete(f"/api/containers/{row.id}", headers=headers).status_code == 503
    assert row.status == "pending_share_approval"
    assert state["calls"] == 0


def test_master_remote_share_provision_uncertainty_is_not_retried(master_share, monkeypatch):
    client, db, _, state = master_share
    headers = user_headers("applicant")
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=headers).status_code == 200
    state["state"] = "approved"
    from app.remote_agent import RemoteAgentError

    def lost_reply(*_):
        state["calls"] += 1
        raise RemoteAgentError("timeout")

    monkeypatch.setattr(containers.RemoteAgentClient, "provision_share", lost_reply)
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "provisioning"
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "provisioning"
    assert state["calls"] == 1
    row = db.query(ContainerModel).first()
    assert client.delete(f"/api/containers/{row.id}", headers=headers).status_code == 409


def test_master_recovers_lost_result_without_reprovision(master_share, monkeypatch):
    client, db, _, state = master_share
    headers = user_headers("applicant")
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=headers).status_code == 200
    state["state"] = "approved"
    from app.remote_agent import RemoteAgentError

    def lost_reply(self, request_id):
        state["calls"] += 1
        state["state"] = "provisioned"
        raise RemoteAgentError("timeout")

    monkeypatch.setattr(containers.RemoteAgentClient, "provision_share", lost_reply)
    row = db.query(ContainerModel).first()
    payload = json.loads(row.pending_share_json)
    occupancy = payload["occupancy"]
    monkeypatch.setattr(containers.RemoteAgentClient, "share_status", lambda self, request_id: {
        "request_id": request_id, "state": state["state"], "occupancy": occupancy,
        "approvers": [{"container_ids": ["owner-id"], "approved": True}],
        **({"ssh_password": "password", "provision_result": {"container_id": "created-id",
           "ssh_port": 22001, "extra_ports": {}}} if state["state"] == "provisioned" else {})})
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "provisioning"
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "running"
    assert state["calls"] == 1


def test_master_recovers_after_db_commit_failure(master_share, monkeypatch):
    client, db, _, state = master_share
    headers = user_headers("applicant")
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=headers).status_code == 200
    state["state"] = "approved"
    original_commit = db.commit
    failed = []

    def fail_once():
        if not failed and db.query(ContainerModel).first().status == "running":
            failed.append(True)
            raise RuntimeError("commit unavailable")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_once)
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "provisioning"
    assert db.query(ContainerModel).first().status == "provisioning"
    monkeypatch.setattr(containers.RemoteAgentClient, "share_status", lambda self, request_id: {
        "request_id": request_id, "state": "provisioned",
        "occupancy": json.loads(db.query(ContainerModel).first().pending_share_json)["occupancy"],
        "ssh_password": "password", "provision_result": {"container_id": "created-id",
            "ssh_port": 22001, "extra_ports": {}}})
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "running"
    assert state["calls"] == 1


def test_auth_and_idempotent_request(worker):
    client, db, _, _ = worker
    path = "/api/agent/v1/share-requests"
    assert client.post(path, json={}).status_code == 401
    assert client.post(path, json={}, headers=user_headers("owner")).status_code == 401
    first = request(client)
    assert first.status_code == 200
    assert first.json()["occupancy"] == [{"container_id": "owner-id", "name": "owner-container",
                                          "username": "owner", "gpu_ids": [0]}]
    assert first.json()["gpu_occupancy_counts"] == {"0": 1}
    assert "user_id" not in first.text
    assert "contact" not in first.text
    assert request(client).json() == first.json()
    assert request(client, name="changed").status_code == 409
    assert db.query(ShareRequestModel).count() == 1
    assert client.get(path + "/" + "a" * 32).status_code == 401


def test_snapshot_is_immutable_and_counts_distinct_users(worker):
    client, db, runtime, _ = worker
    db.add(ContainerModel(container_id="second-id", name="second-container", node_id=agent.NODE_ID,
                          user_id=1, gpu_ids="0,1", status="running", ssh_port=22003,
                          expires_at=datetime.now() + timedelta(days=2)))
    db.commit()
    runtime.append({"container_id": "second-id", "name": "second-container", "username": "owner",
                    "status": "running", "gpu_ids": "0,1"})
    first = request(client, gpu_ids=[0, 1])
    assert first.status_code == 200
    assert first.json()["gpu_occupancy_counts"] == {"0": 1, "1": 1}
    assert len(first.json()["occupancy"]) == 2
    assert len(first.json()["approvers"]) == 1
    assert "user_id" not in first.text
    runtime.pop()
    path = "/api/agent/v1/share-requests/" + "a" * 32
    headers = {"Authorization": "Bearer secret"}
    assert client.get(path, headers=headers).json() == first.json()
    assert request(client, gpu_ids=[0, 1]).json() == first.json()
    assert request(client, gpu_ids=[0]).status_code == 409
    assert client.get(path, headers=user_headers("owner")).status_code == 401


@pytest.mark.parametrize("approved", [False, True])
def test_cancel_is_authenticated_idempotent_and_cannot_replay(worker, approved):
    client, db, _, _ = worker
    assert request(client).status_code == 200
    if approved:
        action = "/api/containers/remote-share-requests/" + "a" * 32
        assert client.post(action + "/approve", headers=user_headers("owner")).json()["state"] == "approved"
    path = "/api/agent/v1/share-requests/" + "a" * 32
    headers = {"Authorization": "Bearer secret"}
    assert client.delete(path).status_code == 401
    assert client.delete(path, headers=user_headers("owner")).status_code == 401
    assert client.delete(path, headers=headers).json()["state"] == "cancelled"
    assert client.delete(path, headers=headers).json()["state"] == "cancelled"
    assert db.get(ShareRequestModel, "a" * 32).state == "cancelled"
    assert client.get(path, headers=headers).json()["state"] == "cancelled"
    assert request(client).json()["state"] == "cancelled"
    assert request(client, name="changed").status_code == 409
    assert client.post(path + "/provision", headers=headers).status_code == 409
    assert client.delete("/api/agent/v1/share-requests/" + "b" * 32, headers=headers).status_code == 404


@pytest.mark.parametrize("state", ["provisioning", "provisioned", "rejected"])
def test_cancel_refuses_non_cancellable_states(worker, state):
    client, db, _, _ = worker
    assert request(client).status_code == 200
    row = db.get(ShareRequestModel, "a" * 32)
    row.state = state
    db.commit()
    path = "/api/agent/v1/share-requests/" + "a" * 32
    assert client.delete(path, headers={"Authorization": "Bearer secret"}).status_code == 409
    assert db.get(ShareRequestModel, "a" * 32).state == state


def test_forgery_approval_and_changed_owner(worker):
    client, db, runtime, _ = worker
    assert request(client).status_code == 200
    path = "/api/containers/remote-share-requests/" + "a" * 32
    assert client.post(path + "/approve").status_code == 401
    assert client.post(path + "/approve", headers=user_headers("stranger"), json={"user_id": 1}).status_code == 403
    assert len(client.get("/api/containers/notifications", headers=user_headers("owner")).json()["items"]) == 1
    runtime[0]["username"] = "stranger"
    assert client.post(path + "/approve", headers=user_headers("owner")).status_code == 409
    runtime[0]["username"] = "owner"
    db.get(ContainerModel, 1).user_id = 2
    db.commit()
    assert client.post(path + "/approve", headers=user_headers("owner")).status_code == 409


def test_unknown_capacity_and_legacy_create_guard(worker):
    client, db, runtime, monkeypatch = worker
    runtime.append({"container_id": "master-id", "name": "master", "username": "remote", "status": "running", "gpu_ids": "0"})
    assert request(client).status_code == 409
    runtime.pop()
    monkeypatch.setattr("app.worker_share.get_setting", lambda *_: "1")
    assert request(client).status_code == 409
    monkeypatch.setattr("app.worker_share.get_setting", lambda *_: "2")
    assert client.post("/api/agent/v1/containers", json={"name": "legacy", "username": "applicant", "gpu_ids": [0]},
                       headers={"Authorization": "Bearer secret"}).status_code == 409
    runtime.clear()
    db.get(ContainerModel, 1).status = "stopped"
    db.commit()
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22001)
    monkeypatch.setattr(agent, "create_container", lambda *_, **__: ("created", "password", {}))
    assert client.post("/api/agent/v1/containers", json={"name": "free", "username": "applicant", "gpu_ids": [1]},
                       headers={"Authorization": "Bearer secret"}).status_code == 200


def test_approved_provision_is_single_create_and_rechecks(worker):
    client, db, runtime, monkeypatch = worker
    assert request(client).status_code == 200
    action = "/api/containers/remote-share-requests/" + "a" * 32
    assert client.post(action + "/approve", headers=user_headers("owner")).json()["state"] == "approved"
    path = "/api/agent/v1/share-requests/" + "a" * 32 + "/provision"
    assert client.post(path, headers=user_headers("owner")).status_code == 401
    runtime.append({"container_id": "unknown", "name": "unknown", "status": "running", "gpu_ids": "0"})
    assert client.post(path, headers={"Authorization": "Bearer secret"}).status_code == 409
    runtime.pop()
    calls = []
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
    def create_shared(*args, **kwargs):
        calls.append((args, kwargs))
        runtime.append({"container_id": "new-id", "name": "new-container", "username": "applicant",
"status": "running", "gpu_ids": "0", "ssh_port": 22002, "extra_ports": {"8888": 30001},
                         "request_id": kwargs["request_id"], "ssh_password": kwargs["ssh_password"]})
        return "new-id", kwargs["ssh_password"], {8888: 30001}

    monkeypatch.setattr(agent, "create_container", create_shared)
    first = client.post(path, headers={"Authorization": "Bearer secret"})
    assert first.status_code == 200
    password = first.json()["ssh_password"]
    assert password == calls[0][1]["ssh_password"]
    second = client.post(path, headers={"Authorization": "Bearer secret"})
    assert second.status_code == 200
    assert second.json()["ssh_password"] == password
    assert "ssh_password" not in second.json()["provision_result"]
    assert password in db.get(ShareRequestModel, "a" * 32).provision_result
    recovered = client.get("/api/agent/v1/share-requests/" + "a" * 32,
                           headers={"Authorization": "Bearer secret"})
    assert recovered.json()["ssh_password"] == password
    assert "ssh_password" not in recovered.json()["provision_result"]
    runtime[-1]["username"] = "other"
    unsafe = client.get("/api/agent/v1/share-requests/" + "a" * 32,
                        headers={"Authorization": "Bearer secret"})
    assert unsafe.json()["state"] == "uncertain"
    assert "password" not in unsafe.text
    runtime[-1]["username"] = "applicant"
    assert client.post(path, headers={"Authorization": "Bearer secret"}).json()["ssh_password"] == password
    assert client.delete("/api/agent/v1/share-requests/" + "a" * 32,
                         headers={"Authorization": "Bearer secret"}).status_code == 409
    assert len(calls) == 1


def test_worker_recovers_crash_after_docker_create_without_duplicate(worker, monkeypatch):
    client, db, runtime, _ = worker
    assert request(client).status_code == 200
    url = "/api/agent/v1/share-requests/" + "a" * 32
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/api/containers/remote-share-requests/" + "a" * 32 + "/approve",
                       headers=user_headers("owner")).status_code == 200
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
    calls = []
    def create_shared(*args, **kwargs):
        calls.append(kwargs)
        runtime.append({"container_id": "new-id", "name": "new-container", "username": "applicant",
"status": "running", "gpu_ids": "0", "ssh_port": 22002, "extra_ports": {"8888": 30001},
                         "request_id": kwargs["request_id"], "ssh_password": kwargs["ssh_password"]})
        return "new-id", kwargs["ssh_password"], {8888: 30001}
    monkeypatch.setattr(agent, "create_container", create_shared)
    original_commit = db.commit
    failed = []
    def fail_after_create():
        row = db.get(ShareRequestModel, "a" * 32)
        if runtime[-1].get("container_id") == "new-id" and row.state == "provisioned" and not failed:
            failed.append(True)
            db.rollback()
            raise RuntimeError("db unavailable")
        original_commit()
    monkeypatch.setattr(db, "commit", fail_after_create)
    with pytest.raises(RuntimeError):
        client.post(url + "/provision", headers=headers)
    assert db.get(ShareRequestModel, "a" * 32).state == "provisioning"
    assert calls[0]["ssh_password"] not in client.post("/api/agent/v1/share-requests", json={
        "request_id": "a" * 32, "applicant": "applicant", "username": "applicant", "name": "new-container",
        "gpu_ids": [0], "lease_days": 3, "mem_limit_gb": 8}, headers=headers).text
    assert client.delete(url, headers=headers).status_code == 409
    original_list = worker_share.list_managed_containers
    monkeypatch.setattr(worker_share, "list_managed_containers", lambda **_: (_ for _ in ()).throw(RuntimeError("offline")))
    assert client.get(url, headers=headers).status_code == 503
    assert db.get(ShareRequestModel, "a" * 32).state == "provisioning"
    monkeypatch.setattr(worker_share, "list_managed_containers", original_list)
    recovered = client.get(url, headers=headers)
    assert recovered.status_code == 200
    assert recovered.json()["ssh_password"] == calls[0]["ssh_password"]
    assert client.post(url + "/provision", headers=headers).json()["ssh_password"] == calls[0]["ssh_password"]
    assert len(calls) == 1


def test_cancel_waits_for_provision_and_cannot_undo_it(worker, monkeypatch):
    client, db, runtime, _ = worker
    assert request(client).status_code == 200
    assert client.post("/api/containers/remote-share-requests/" + "a" * 32 + "/approve",
                       headers=user_headers("owner")).status_code == 200
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
    entered = Event()
    release = Event()
    def create_shared(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        runtime.append({"container_id": "new-id", "name": "new-container", "username": "applicant",
"status": "running", "gpu_ids": "0", "ssh_port": 22002, "extra_ports": {"8888": 30001},
                         "request_id": kwargs["request_id"], "ssh_password": kwargs["ssh_password"]})
        return "new-id", kwargs["ssh_password"], {8888: 30001}
    monkeypatch.setattr(agent, "create_container", create_shared)
    url = "/api/agent/v1/share-requests/" + "a" * 32
    headers = {"Authorization": "Bearer secret"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        provision = pool.submit(client.post, url + "/provision", headers=headers)
        assert entered.wait(5)
        cancel = pool.submit(client.delete, url, headers=headers)
        release.set()
        assert provision.result(timeout=5).status_code == 200
        assert cancel.result(timeout=5).status_code == 409
    assert db.get(ShareRequestModel, "a" * 32).state == "provisioned"


def test_worker_transient_runtime_failure_is_retryable(worker, monkeypatch):
    client, db, runtime, _ = worker
    assert request(client).status_code == 200
    headers = {"Authorization": "Bearer secret"}
    url = "/api/agent/v1/share-requests/" + "a" * 32
    assert client.post("/api/containers/remote-share-requests/" + "a" * 32 + "/approve",
                       headers=user_headers("owner")).status_code == 200
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
    calls = []
    def create_shared(*args, **kwargs):
        calls.append(kwargs)
        runtime.append({"container_id": "new-id", "name": "new-container", "username": "applicant",
"status": "running", "gpu_ids": "0", "ssh_port": 22002, "extra_ports": {"8888": 30001},
                         "request_id": kwargs["request_id"], "ssh_password": kwargs["ssh_password"]})
        return "new-id", kwargs["ssh_password"], {8888: 30001}
    monkeypatch.setattr(agent, "create_container", create_shared)
    assert client.post(url + "/provision", headers=headers).status_code == 200
    original = worker_share.list_managed_containers
    monkeypatch.setattr(worker_share, "list_managed_containers", lambda **_: (_ for _ in ()).throw(RuntimeError("docker offline")))
    assert client.get(url, headers=headers).status_code == 503
    assert db.get(ShareRequestModel, "a" * 32).state == "provisioned"
    monkeypatch.setattr(worker_share, "list_managed_containers", original)
    assert client.get(url, headers=headers).json()["ssh_password"] == calls[0]["ssh_password"]
    assert len(calls) == 1


def test_worker_ambiguous_recovery_fails_closed(worker, monkeypatch):
    client, db, runtime, _ = worker
    assert request(client).status_code == 200
    row = db.get(ShareRequestModel, "a" * 32)
    row.state = "provisioning"
    row.provision_result = json.dumps({"ssh_port": 22002, "ssh_password": "saved-secret"})
    db.commit()
    runtime.append({"container_id": "unlabelled", "name": "new-container", "username": "applicant",
                    "status": "running", "gpu_ids": "0", "ssh_port": 22002, "extra_ports": {}})
    monkeypatch.setattr(agent, "create_container", lambda *args, **kwargs: pytest.fail("duplicate create"))
    url = "/api/agent/v1/share-requests/" + "a" * 32
    headers = {"Authorization": "Bearer secret"}
    response = client.get(url, headers=headers)
    assert response.json()["state"] == "uncertain"
    assert "saved-secret" not in response.text
    assert client.post(url + "/provision", headers=headers).json()["state"] == "uncertain"


def test_merge_guard_and_changed_owner_after_approval(worker):
    client, db, runtime, _ = worker
    merge = {"name": "master-container", "username": "applicant", "old_gpu_ids": [1],
             "gpu_ids": [0, 1], "ssh_port": 22001, "extra_ports": {},
             "ssh_password_hash": "0" * 64, "mem_limit_gb": 8}
    assert client.post("/api/agent/v1/containers/master-id/merge", json=merge,
                       headers={"Authorization": "Bearer secret"}).status_code == 409
    assert request(client).status_code == 200
    action = "/api/containers/remote-share-requests/" + "a" * 32
    assert client.post(action + "/approve", headers=user_headers("owner")).status_code == 200
    runtime[0]["container_id"] = "replacement"
    assert client.post("/api/agent/v1/share-requests/" + "a" * 32 + "/provision",
                       headers={"Authorization": "Bearer secret"}).status_code == 409
    assert db.get(ShareRequestModel, "a" * 32).state == "approved"


def test_expiry_and_rejection(worker):
    client, db, _, _ = worker
    assert request(client).status_code == 200
    row = db.get(ShareRequestModel, "a" * 32)
    row.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    path = "/api/containers/remote-share-requests/" + "a" * 32
    assert client.post(path + "/approve", headers=user_headers("owner")).status_code == 409
    assert db.get(ShareRequestModel, "a" * 32).state == "expired"
    assert client.post(path + "/reject", headers=user_headers("owner")).status_code == 409
    assert client.post("/api/agent/v1/share-requests/" + "a" * 32 + "/provision",
                       headers={"Authorization": "Bearer secret"}).status_code == 409


@pytest.mark.parametrize("state, expected", [("approved", "expired"), ("provisioning", "uncertain")])
def test_worker_expired_request_is_terminal(worker, state, expected):
    client, db, _, monkeypatch = worker
    assert request(client).status_code == 200
    row = db.get(ShareRequestModel, "a" * 32)
    row.state = state
    row.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    url = "/api/agent/v1/share-requests/" + "a" * 32
    headers = {"Authorization": "Bearer secret"}
    assert client.get(url, headers=headers).json()["state"] == expected
    assert db.get(ShareRequestModel, "a" * 32).state == expected
    provision = client.post(url + "/provision", headers=headers)
    assert provision.status_code == (409 if state == "approved" else 200)
    if state == "provisioning":
        assert provision.json()["state"] == "uncertain"
    assert client.delete(url, headers=headers).status_code == 409
    assert request(client).json()["state"] == expected


def test_master_reconciles_worker_terminal_states(master_share):
    client, db, _, state = master_share
    headers = user_headers("applicant")
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=headers).status_code == 200
    state["state"] = "expired"
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "share_rejected"
    assert state["calls"] == 0


def test_master_uncertain_provision_is_terminal(master_share):
    client, db, _, state = master_share
    headers = user_headers("applicant")
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=headers).status_code == 200
    row = db.query(ContainerModel).first()
    row.status = "provisioning"
    db.commit()
    state["state"] = "uncertain"
    assert client.get("/api/containers/my", headers=headers).json()[0]["status"] == "share_uncertain"
    assert client.delete(f"/api/containers/{row.id}", headers=headers).status_code == 409
    assert state["calls"] == 0


def test_master_scheduler_registers_remote_reconciliation(monkeypatch):
    jobs = {}

    class Scheduler:
        running = False

        def add_job(self, func, trigger, **kwargs):
            jobs[kwargs["id"]] = (func, trigger, kwargs)

        def start(self):
            self.running = True

    monkeypatch.setattr(scheduler, "scheduler", Scheduler())
    monkeypatch.setattr(scheduler, "NODE_ROLE", "master")
    scheduler.start_scheduler()
    func, trigger, options = jobs["reconcile-remote-shares"]
    assert func is scheduler._reconcile_pending_remote_shares
    assert trigger.interval.total_seconds() == 60
    assert options["max_instances"] == 1 and options["coalesce"]
    jobs.clear()
    scheduler.scheduler.running = False
    monkeypatch.setattr(scheduler, "NODE_ROLE", "worker")
    scheduler.start_scheduler()
    assert "reconcile-remote-shares" not in jobs


def test_master_scheduler_reconciles_without_applicant_poll(master_share, monkeypatch):
    client, db, _, state = master_share
    monkeypatch.setattr(scheduler, "NODE_ROLE", "master")
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=user_headers("applicant")).status_code == 200
    state["state"] = "approved"
    scheduler._reconcile_pending_remote_shares()
    assert db.query(ContainerModel).first().status == "running"
    scheduler._reconcile_pending_remote_shares()
    assert state["calls"] == 1


@pytest.mark.parametrize("guard", ["containers", "gpus", "disabled", "unschedulable", "merge"])
def test_master_delayed_remote_provision_rechecks_limits_and_node(master_share, monkeypatch, guard):
    client, db, _, state = master_share
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=user_headers("applicant")).status_code == 200
    row = db.query(ContainerModel).first()
    node = db.get(ComputeNodeModel, "worker-1")
    if guard == "containers":
        monkeypatch.setattr(containers, "MAX_CONTAINERS_PER_USER", 1)
        db.add(ContainerModel(container_id="other-id", name="other", user_id=1, gpu_ids="",
            node_id="worker-1", ssh_port=22002, status="running", expires_at=datetime.now() + timedelta(days=1)))
    elif guard == "gpus":
        db.get(UserModel, 1).max_gpus_per_user = 1
        db.add(ContainerModel(container_id="other-id", name="other", user_id=1, gpu_ids="1",
            node_id="worker-1", ssh_port=22002, status="running", expires_at=datetime.now() + timedelta(days=1)))
    elif guard == "disabled":
        node.enabled = False
    elif guard == "unschedulable":
        node.schedulable = False
    else:
        row.target_container_id = row.id
    db.commit()
    state["state"] = "approved"
    containers._reconcile_remote_shares(db, 1)
    assert row.status == "pending_share_approval"
    assert state["calls"] == 0
    if guard == "containers" or guard == "gpus":
        db.query(ContainerModel).filter(ContainerModel.container_id == "other-id").delete()
    elif guard == "disabled":
        node.enabled = True
    elif guard == "unschedulable":
        node.schedulable = True
    else:
        row.target_container_id = None
    db.commit()
    containers._reconcile_remote_shares(db, 1)
    assert row.status == "running"
    assert state["calls"] == 1


def test_remote_share_quota_notification_commits_with_provision(master_share, monkeypatch):
    client, db, _, state = master_share
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=user_headers("applicant")).status_code == 200
    monkeypatch.setattr(containers, "check_user_can_provision", quota_service.check_user_can_provision)
    monkeypatch.setattr(quota_service, "workspace_usage_for_user_result",
                        lambda _: quota_service.WorkspaceUsage(90, True))
    user = db.get(UserModel, 1)
    user.disk_quota_bytes = 100
    db.commit()
    state["state"] = "approved"

    containers._reconcile_remote_shares(db, user.id)

    db.expire_all()
    assert state["calls"] == 1
    assert db.query(ContainerModel).first().status == "running"
    assert db.get(UserModel, user.id).disk_notification_band == 90
    assert [event.type for event in db.query(UserNotificationModel).all()] == ["disk_usage_90"]


def test_remote_share_rechecks_stored_limit(master_share):
    client, db, _, state = master_share
    user = db.get(UserModel, 1)
    user.max_gpus_per_user = 3
    db.commit()
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=user_headers("applicant")).status_code == 200
    row = db.query(ContainerModel).first()
    user.max_gpus_per_user = 0
    db.commit()
    state["state"] = "approved"
    containers._reconcile_remote_shares(db, user.id)
    assert row.status == "pending_share_approval"
    assert state["calls"] == 0
    user.max_gpus_per_user = 1
    db.commit()
    containers._reconcile_remote_shares(db, user.id)
    assert row.status == "running"
    assert state["calls"] == 1


def test_master_scheduler_reconciles_rejection(master_share, monkeypatch):
    client, db, _, state = master_share
    monkeypatch.setattr(scheduler, "NODE_ROLE", "master")
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=user_headers("applicant")).status_code == 200
    state["state"] = "rejected"
    scheduler._reconcile_pending_remote_shares()
    assert db.query(ContainerModel).first().status == "share_rejected"
    assert state["calls"] == 0


def test_master_uncertain_recovers_on_scheduler_retry(master_share, monkeypatch):
    client, db, _, state = master_share
    assert client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 3}, headers=user_headers("applicant")).status_code == 200
    row = db.query(ContainerModel).first()
    row.status = "provisioning"
    db.commit()
    state["state"] = "uncertain"
    monkeypatch.setattr(scheduler, "NODE_ROLE", "master")
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    scheduler._reconcile_pending_remote_shares()
    assert row.status == "share_uncertain"
    state["state"] = "provisioned"
    original = containers.RemoteAgentClient.recover_share

    def recovered(self, request_id):
        return {**original(self, request_id), "ssh_password": "password",
                "provision_result": {"container_id": "created-id", "ssh_port": 22001, "extra_ports": {}}}

    monkeypatch.setattr(containers.RemoteAgentClient, "recover_share", recovered)
    scheduler._reconcile_pending_remote_shares()
    assert row.status == "running"
    assert state["calls"] == 0


def test_worker_lock_is_reentrant_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_share, "NODE_ROLE", "worker")
    monkeypatch.setattr(worker_share, "DATA_DIR", tmp_path)
    with worker_share.worker_gpu_lock:
        with worker_share.worker_gpu_lock:
            assert (tmp_path / "worker-gpu.lock").stat().st_mode & 0o077 == 0
    os.chmod(tmp_path / "worker-gpu.lock", 0o644)
    with pytest.raises(RuntimeError):
        with worker_share.worker_gpu_lock:
            pass


def test_username_collision_at_request_and_provision(worker, tmp_path):
    client, db, runtime, monkeypatch = worker
    (tmp_path / "applicant").mkdir()
    (tmp_path / "applicant" / "local-data").write_text("private")
    assert request(client).status_code == 200
    action = "/api/containers/remote-share-requests/" + "a" * 32
    assert client.post(action + "/approve", headers=user_headers("owner")).status_code == 200
    db.add(UserModel(username="applicant", hashed_password="x"))
    db.commit()
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
    paths = []
    def create_shared(*args, **kwargs):
        paths.append(kwargs["workspace_path"])
        runtime.append({"container_id": "new-id", "name": "new-container", "username": "applicant",
"status": "running", "gpu_ids": "0", "ssh_port": 22002, "extra_ports": {"8888": 30001},
                         "request_id": kwargs["request_id"], "ssh_password": kwargs["ssh_password"]})
        return "new-id", kwargs["ssh_password"], {8888: 30001}

    monkeypatch.setattr(agent, "create_container", create_shared)
    path = "/api/agent/v1/share-requests/" + "a" * 32 + "/provision"
    assert client.post(path, headers={"Authorization": "Bearer secret"}).status_code == 200
    assert paths == [str(tmp_path / ".compute-graveyard-master" / "applicant")]
    assert (tmp_path / "applicant" / "local-data").read_text() == "private"


def test_agent_create_allows_existing_worker_admin_with_isolated_workspace(worker, tmp_path):
    client, db, runtime, monkeypatch = worker
    db.add(UserModel(username="admin", hashed_password="x", role="admin"))
    db.commit()
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin" / "local-data").write_text("private")
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
    paths = []
    monkeypatch.setattr(agent, "create_container", lambda *args, **kwargs: paths.append(kwargs["workspace_path"]) or ("new-id", "password", {}))
    response = client.post("/api/agent/v1/containers", json={"name": "admin-remote", "username": "admin", "gpu_ids": [1]},
                           headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert paths == [str(tmp_path / ".compute-graveyard-master" / "admin")]
    assert (tmp_path / "admin" / "local-data").read_text() == "private"


def test_docker_creation_preserves_username_label_with_master_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_service, "USER_DATA_BASE", tmp_path)
    monkeypatch.setattr(docker_service, "allocate_service_ports", lambda: {8888: 30001})
    calls = []
    client = SimpleNamespace(images=SimpleNamespace(get=lambda _: None),
                             containers=SimpleNamespace(run=lambda *args, **kwargs: calls.append(kwargs) or SimpleNamespace(id="created")))
    monkeypatch.setattr(docker_service, "get_docker_client", lambda: client)
    workspace = tmp_path / ".compute-graveyard-master" / "admin"
    workspace.mkdir(parents=True)
    docker_service.create_container("remote-admin", "admin", [0], 22002, workspace_path=str(workspace))
    assert calls[0]["volumes"][str(workspace)]["bind"] == "/workspace"
    assert calls[0]["labels"]["compute-graveyard.username"] == "admin"
    assert not (tmp_path / "admin").exists()


def test_docker_request_identity_is_not_in_default_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_service, "USER_DATA_BASE", tmp_path)
    monkeypatch.setattr(docker_service, "allocate_service_ports", lambda: {8888: 30001})
    calls = []
    runtime = SimpleNamespace(id="created", name="shared", status="running", attrs={
        "NetworkSettings": {"Ports": {"22/tcp": [{"HostPort": "22002"}]}},
        "Config": {"Labels": {"compute-graveyard.managed": "true",
                              "compute-graveyard.username": "applicant",
                              "compute-graveyard.gpu_ids": "0",
                              "compute-graveyard.request_id": "a" * 32}}})
    client = SimpleNamespace(images=SimpleNamespace(get=lambda _: None),
        containers=SimpleNamespace(run=lambda *args, **kwargs: calls.append(kwargs) or runtime,
                                   list=lambda **kwargs: [runtime]))
    monkeypatch.setattr(docker_service, "get_docker_client", lambda: client)
    docker_service.create_container("shared", "applicant", [0], 22002,
                                    ssh_password="saved-secret", request_id="a" * 32)
    assert calls[0]["environment"]["SSH_PASSWORD"] == "saved-secret"
    assert calls[0]["labels"]["compute-graveyard.request_id"] == "a" * 32
    assert "request_id" not in docker_service.list_managed_containers()[0]
    assert docker_service.list_managed_containers(include_request_id=True)[0]["request_id"] == "a" * 32


@pytest.mark.parametrize("allocator", ["allocate_ssh_port", "allocate_service_ports"])
def test_docker_port_list_failure_never_allocates(monkeypatch, allocator):
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda **kwargs: (_ for _ in ()).throw(
        docker_service.DockerException("offline"))))
    monkeypatch.setattr(docker_service, "get_docker_client", lambda: client)
    with pytest.raises(RuntimeError, match="端口占用"):
        getattr(docker_service, allocator)()


@pytest.mark.parametrize("failure", ["ssh", "service", "exhausted"])
def test_port_probe_failure_keeps_share_approved(worker, failure):
    client, db, _, monkeypatch = worker
    assert request(client).status_code == 200
    assert client.post("/api/containers/remote-share-requests/" + "a" * 32 + "/approve",
                       headers=user_headers("owner")).status_code == 200
    monkeypatch.setattr(agent, "create_container", lambda *args, **kwargs: pytest.fail("Docker create started"))
    if failure == "ssh":
        monkeypatch.setattr(agent, "allocate_ssh_port", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    elif failure == "service":
        monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
        monkeypatch.setattr(agent, "allocate_service_ports", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    else:
        monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
        monkeypatch.setattr(agent, "allocate_service_ports", lambda: None)
    url = "/api/agent/v1/share-requests/" + "a" * 32
    headers = {"Authorization": "Bearer secret"}
    assert client.post(url + "/provision", headers=headers).status_code == 503
    row = db.get(ShareRequestModel, "a" * 32)
    assert row.state == "approved" and row.provision_result is None
    assert client.get(url, headers=headers).json()["state"] == "approved"


def test_expired_provisioning_recovers_after_runtime_outage(worker, monkeypatch):
    client, db, runtime, _ = worker
    assert request(client).status_code == 200
    row = db.get(ShareRequestModel, "a" * 32)
    row.state = "provisioning"
    row.expires_at = datetime.now() - timedelta(hours=25)
    row.provision_result = json.dumps({"ssh_port": 22002, "ssh_password": "saved-secret",
                                       "extra_ports": {"8888": 30001}})
    db.commit()
    runtime.append({"container_id": "new-id", "name": "new-container", "username": "applicant",
                    "status": "running", "gpu_ids": "0", "ssh_port": 22002,
                    "extra_ports": {"8888": 30001}, "request_id": "a" * 32,
                    "ssh_password": "saved-secret"})
    monkeypatch.setattr(agent, "create_container", lambda *args, **kwargs: pytest.fail("duplicate create"))
    url = "/api/agent/v1/share-requests/" + "a" * 32
    headers = {"Authorization": "Bearer secret"}
    original = worker_share.list_managed_containers
    monkeypatch.setattr(worker_share, "list_managed_containers", lambda **_: (_ for _ in ()).throw(RuntimeError("offline")))
    assert client.get(url, headers=headers).status_code == 503
    assert db.get(ShareRequestModel, "a" * 32).state == "provisioning"
    monkeypatch.setattr(worker_share, "list_managed_containers", original)
    recovered = client.get(url, headers=headers)
    assert recovered.status_code == 200
    assert recovered.json()["state"] == "provisioned"
    assert recovered.json()["ssh_password"] == "saved-secret"
    assert client.post(url + "/provision", headers=headers).json()["ssh_password"] == "saved-secret"


def test_uncertain_request_recovers_on_later_status(worker):
    client, db, runtime, _ = worker
    assert request(client).status_code == 200
    row = db.get(ShareRequestModel, "a" * 32)
    row.state = "provisioning"
    row.expires_at = datetime.now() - timedelta(hours=25)
    row.provision_result = json.dumps({"ssh_port": 22002, "ssh_password": "saved-secret"})
    db.commit()
    url = "/api/agent/v1/share-requests/" + "a" * 32
    headers = {"Authorization": "Bearer secret"}
    assert client.get(url, headers=headers).json()["state"] == "uncertain"
    runtime.append({"container_id": "new-id", "name": "new-container", "username": "applicant",
                    "status": "running", "gpu_ids": "0", "ssh_port": 22002,
                    "extra_ports": {}, "request_id": "a" * 32, "ssh_password": "saved-secret"})
    assert client.get(url, headers=headers).json()["ssh_password"] == "saved-secret"


def test_master_admin_workspace_is_isolated_from_worker_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_share, "USER_DATA_BASE", tmp_path)
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin" / "worker.txt").write_text("local")
    workspace = worker_share.master_workspace("admin")
    assert workspace == str(tmp_path / ".compute-graveyard-master" / "admin")
    assert (tmp_path / "admin" / "worker.txt").read_text() == "local"
    assert worker_share.master_workspace("admin") == workspace
    (tmp_path / ".compute-graveyard-master" / "stranger").symlink_to(tmp_path / "admin")
    with pytest.raises(RuntimeError):
        worker_share.master_workspace("stranger")


def test_no_port_stays_retryable(worker):
    client, db, runtime, monkeypatch = worker
    assert request(client).status_code == 200
    action = "/api/containers/remote-share-requests/" + "a" * 32
    assert client.post(action + "/approve", headers=user_headers("owner")).status_code == 200
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: None)
    path = "/api/agent/v1/share-requests/" + "a" * 32 + "/provision"
    assert client.post(path, headers={"Authorization": "Bearer secret"}).status_code == 503
    assert db.get(ShareRequestModel, "a" * 32).state == "approved"
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
    def create_shared(*args, **kwargs):
        runtime.append({"container_id": "new-id", "name": "new-container", "username": "applicant",
"status": "running", "gpu_ids": "0", "ssh_port": 22002, "extra_ports": {"8888": 30001},
                         "request_id": kwargs["request_id"], "ssh_password": kwargs["ssh_password"]})
        return "new-id", kwargs["ssh_password"], {8888: 30001}

    monkeypatch.setattr(agent, "create_container", create_shared)
    assert client.post(path, headers={"Authorization": "Bearer secret"}).json()["ssh_password"] == runtime[-1]["ssh_password"]


def test_direct_create_rejects_unknown_and_master_occupancy(worker):
    client, _, runtime, monkeypatch = worker
    body = {"name": "new", "username": "applicant", "gpu_ids": [1]}
    url = "/api/agent/v1/containers"
    headers = {"Authorization": "Bearer secret"}
    monkeypatch.setattr(agent, "create_container", lambda *_, **__: ("new-id", "password", {}))
    monkeypatch.setattr(agent, "allocate_ssh_port", lambda: 22002)
    runtime.append({"container_id": "untracked", "name": "other", "username": "other", "status": "running", "gpu_ids": "1"})
    assert client.post(url, json=body, headers=headers).status_code == 409
    runtime.pop()
    runtime.append({"container_id": "master-id", "name": "master", "username": "other", "status": "running", "gpu_ids": "1"})
    assert client.post(url, json=body, headers=headers).status_code == 409
    runtime.pop()
    assert client.post(url, json={**body, "username": "owner"}, headers=headers).status_code == 200
    assert client.post(url, json=body, headers=headers).status_code == 200


def test_merge_rejects_mixed_occupied_gpu_and_allows_same_target(worker):
    client, db, runtime, monkeypatch = worker
    runtime.append({"container_id": "master-id", "name": "master-container", "username": "applicant",
                    "status": "running", "gpu_ids": "1"})
    body = {"name": "master-container", "username": "applicant", "old_gpu_ids": [1],
            "gpu_ids": [1, 0], "ssh_port": 22001, "extra_ports": {},
            "ssh_password_hash": "0" * 64, "mem_limit_gb": 8}
    path = "/api/agent/v1/containers/master-id/merge"
    headers = {"Authorization": "Bearer secret"}
    monkeypatch.setattr(agent, "is_managed_container", lambda *_: True)
    monkeypatch.setattr(agent, "merge_container_gpus", lambda *_, **__: "replacement")
    assert client.post(path, json=body, headers=headers).status_code == 409
    runtime.pop(0)
    db.get(ContainerModel, 1).status = "stopped"
    db.commit()
    assert client.post(path, json=body, headers=headers).status_code == 200
    runtime.append({"container_id": "unknown", "name": "unknown", "status": "running", "gpu_ids": "0"})
    assert client.post(path, json=body, headers=headers).status_code == 409
