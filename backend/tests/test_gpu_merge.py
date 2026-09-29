import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect
from sqlalchemy.orm import Session

from app.api import admin, agent, containers, dashboard
from app import remote_agent, scheduler
from app.database import Base, _migrate_container_merge
from app.database_models import ComputeNodeModel, ContainerModel, UserModel
from app.models import ContainerApplyRequest
from app import docker_service
from docker.errors import NotFound
from pydantic import ValidationError


@pytest.fixture
def state(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    user = UserModel(username="tester", hashed_password="x", role="user")
    other = UserModel(username="other", hashed_password="x", role="user")
    node = ComputeNodeModel(id="local", name="Local", public_host="localhost")
    db.add_all([user, other, node])
    db.commit()
    target = ContainerModel(container_id="old", name="labgpu-tester", user_id=user.id, node_id="local", gpu_ids="0", ssh_port=22001, ssh_password="secret", extra_ports=json.dumps({"8080": 30001}), status="running", expires_at=datetime.now() + timedelta(days=2))
    db.add(target)
    db.commit()
    monkeypatch.setattr(containers, "NODE_ID", "local")
    monkeypatch.setattr(containers, "check_user_can_provision", lambda *a, **k: SimpleNamespace(allowed=True, scan_complete=True))
    monkeypatch.setattr(containers, "select_node", lambda *a, **k: (node, {}))
    monkeypatch.setattr(containers, "get_node", lambda *a, **k: node)
    monkeypatch.setattr(containers, "_max_gpu_sharing", lambda *a: 4)
    monkeypatch.setattr(containers, "get_setting", lambda *a: "32")
    yield db, user, other, target
    db.close()
    engine.dispose()


def test_merge_rejects_other_owner_cpu_and_overlap(state):
    db, user, other, target = state
    for req, actor in [(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), other), (ContainerApplyRequest(target_container_id=target.id, cpu_only=True), user), (ContainerApplyRequest(target_container_id=target.id, gpu_ids=[0]), user)]:
        with pytest.raises(HTTPException) as exc:
            containers.apply_container(req, user=actor, db=db)
        assert exc.value.status_code == 400


def test_merge_quota_counts_only_added_gpus(state, monkeypatch):
    db, user, other, target = state
    user.max_gpus_per_user = 2
    db.commit()
    monkeypatch.setattr(containers, "_merge_action", lambda node, action, old_id, data: {"container_id": "new"} if action == "merge" else None)
    result = containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db)
    assert result.container.id == target.id
    assert target.container_id == "new"
    assert target.gpu_ids == "0,1"
    assert target.expires_at < datetime.now() + timedelta(days=3)
    with pytest.raises(HTTPException):
        containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[2]), user=user, db=db)


def test_gpu_limit_is_per_user_for_apply_and_cpu_only(state, monkeypatch):
    db, user, other, target = state
    user.max_gpus_per_user = 1
    other.max_gpus_per_user = 3
    db.commit()
    with pytest.raises(HTTPException, match="最多使用 1"):
        containers.apply_container(ContainerApplyRequest(gpu_ids=[1]), user=user, db=db)
    monkeypatch.setattr(containers, "provision_on_node", lambda *args: {"container_id": "cpu-id", "ssh_port": 22002})
    assert containers.apply_container(ContainerApplyRequest(cpu_only=True), user=user, db=db).container.gpu_ids == ""
    user.max_gpus_per_user = 0
    db.commit()
    with pytest.raises(HTTPException, match="最多使用 0"):
        containers.apply_container(ContainerApplyRequest(gpu_ids=[1]), user=user, db=db)
    monkeypatch.setattr(containers, "provision_on_node", lambda *args: {"container_id": "gpu-id", "ssh_port": 22003})
    assert containers.apply_container(ContainerApplyRequest(gpu_ids=[1, 2, 3]), user=other, db=db).container.gpu_ids == "1,2,3"
    with pytest.raises(HTTPException, match="最多使用 3"):
        containers.apply_container(ContainerApplyRequest(gpu_ids=[4]), user=other, db=db)


def test_provisioning_remote_share_reserves_gpu_quota(state):
    db, user, other, target = state
    user.max_gpus_per_user = 2
    db.add(ContainerModel(container_id="pending-remote", name="remote", user_id=user.id, node_id="remote", gpu_ids="1", ssh_port=0, status="provisioning", expires_at=datetime.now() + timedelta(days=2)))
    db.commit()
    with pytest.raises(HTTPException, match="最多使用 2"):
        containers.apply_container(ContainerApplyRequest(gpu_ids=[2]), user=user, db=db)
    remote = db.query(ContainerModel).filter_by(container_id="pending-remote").one()
    remote.status = "share_uncertain"
    db.commit()
    with pytest.raises(HTTPException, match="最多使用 2"):
        containers.apply_container(ContainerApplyRequest(gpu_ids=[2]), user=user, db=db)


def test_approval_rechecks_applicant_stored_gpu_limit(state, monkeypatch):
    db, user, other, target = state
    db.add(ContainerModel(container_id="other", name="other", user_id=other.id, node_id="local", gpu_ids="1", ssh_port=22002, status="running", expires_at=datetime.now() + timedelta(days=2)))
    db.commit()
    pending_id = containers.apply_container(ContainerApplyRequest(gpu_ids=[1]), user=user, db=db).container.id
    user.max_gpus_per_user = 1
    db.commit()
    with pytest.raises(HTTPException, match="最多使用 1"):
        containers.approve_share(pending_id, user=other, db=db)
    db.refresh(db.get(ContainerModel, pending_id))
    assert not json.loads(db.get(ContainerModel, pending_id).pending_share_json)["approvers"][0]["approved"]
    user.max_gpus_per_user = 2
    db.commit()
    monkeypatch.setattr(containers, "provision_on_node", lambda *args: {"container_id": "approved-id", "ssh_port": 22003})
    assert containers.approve_share(pending_id, user=other, db=db)["message"] == "已全部同意，容器已创建"


def test_pending_merge_rechecks_occupiers_and_target(state, monkeypatch):
    db, user, other, target = state
    occupier = ContainerModel(container_id="other", name="other", user_id=other.id, node_id="local", gpu_ids="1", ssh_port=22002, status="running", expires_at=datetime.now() + timedelta(days=2))
    db.add(occupier)
    db.commit()
    monkeypatch.setattr(containers, "_merge_action", lambda node, action, old_id, data: {"container_id": "new"} if action == "merge" else None)
    result = containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db)
    pending = db.get(ContainerModel, result.container.id)
    assert pending.target_container_id == target.id
    assert pending.status == "pending_share_approval"
    target.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    with pytest.raises(HTTPException):
        containers.approve_share(pending.id, user=other, db=db)
    assert pending.status == "share_rejected"
    target.expires_at = datetime.now() + timedelta(days=2)
    db.commit()
    result = containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db)
    pending = db.get(ContainerModel, result.container.id)
    assert "已合并" in containers.approve_share(pending.id, user=other, db=db)["message"]
    assert target.container_id == "new"
    assert pending.status == "removed"


def test_failed_runtime_restores_old_record(state, monkeypatch):
    db, user, other, target = state
    calls = []

    def action(node, operation, old_id, data):
        calls.append(operation)
        if operation == "merge":
            raise RuntimeError("docker failed")

    monkeypatch.setattr(containers, "_merge_action", action)
    with pytest.raises(HTTPException):
        containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db)
    db.refresh(target)
    assert calls == ["merge", "rollback"]
    assert target.status == "running"
    assert target.container_id == "old"
    assert target.pending_share_json is None


def test_database_commit_failure_rolls_back_runtime(state, monkeypatch):
    db, user, other, target = state
    calls = []
    monkeypatch.setattr(containers, "_merge_action", lambda node, action, old_id, data: calls.append(action) or ({"container_id": "new"} if action == "merge" else None))
    real_commit = db.commit
    failed = False

    def commit():
        nonlocal failed
        if not failed and target.status == "merging" and target.container_id == "new":
            failed = True
            raise RuntimeError("database unavailable")
        return real_commit()

    monkeypatch.setattr(db, "commit", commit)
    with pytest.raises(HTTPException):
        containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db)
    db.refresh(target)
    assert calls == ["merge", "rollback"]
    assert target.status == "running"
    assert target.container_id == "old"


def test_failed_rollback_stays_in_recoverable_state(state, monkeypatch):
    db, user, other, target = state
    monkeypatch.setattr(containers, "_merge_action", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(HTTPException):
        containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db)
    db.refresh(target)
    assert target.status == "merging"
    assert json.loads(target.pending_share_json)["old_id"] == "old"


def test_docker_start_failure_preserves_original_container(monkeypatch):
    class FakeContainer:
        def __init__(self, name, container_id, labels, fail_start=False):
            self.name = name
            self.id = container_id
            self.status = "running" if not fail_start else "created"
            self.fail_start = fail_start
            self.attrs = {"Config": {"Labels": labels, "Env": ["SSH_PASSWORD=secret"], "Cmd": ["sshd"]}, "HostConfig": {"NetworkMode": "bridge", "PortBindings": {"22/tcp": [{"HostPort": "22001"}]}, "ShmSize": 1024}, "Mounts": [{"Destination": "/workspace", "Source": "/users/tester", "RW": True, "Type": "bind"}]}

        def reload(self):
            pass

        def stop(self):
            self.status = "exited"

        def start(self):
            if self.fail_start:
                raise RuntimeError("cannot start")
            self.status = "running"

        def rename(self, name):
            self.name = name

        def commit(self, **kwargs):
            assert kwargs["changes"] == "ENV SSH_PASSWORD="
            return SimpleNamespace(id="image", attrs={"Config": {"Env": ["SSH_PASSWORD="]}})

        def remove(self, force=False):
            client.items.remove(self)

    class FakeContainers:
        def get(self, identifier):
            for item in client.items:
                if identifier in {item.id, item.name}:
                    return item
            raise NotFound("missing")

        def create(self, image, **kwargs):
            item = FakeContainer(kwargs["name"], "new", kwargs["labels"], fail_start=True)
            client.items.append(item)
            return item

    old = FakeContainer("labgpu-tester", "old", {"compute-graveyard.managed": "true", "compute-graveyard.username": "tester", "compute-graveyard.gpu_ids": "0"})
    client = SimpleNamespace(items=[old], containers=FakeContainers(), images=SimpleNamespace(remove=lambda *args: None))
    monkeypatch.setattr(docker_service, "USER_DATA_BASE", "/users")
    monkeypatch.setattr(docker_service, "get_docker_client", lambda: client)
    import hashlib
    with pytest.raises(RuntimeError, match="原容器已恢复"):
        docker_service.merge_container_gpus("old", "labgpu-tester", "tester", [0], [0, 1], 22001, {}, hashlib.sha256(b"secret").hexdigest(), 64)
    assert old.status == "running"
    assert old.name == "labgpu-tester"
    assert client.items == [old]


@pytest.mark.parametrize("old_starts", [True, False])
def test_rollback_preserves_started_replacement_and_writes(monkeypatch, old_starts):
    operations = []

    class FakeContainer:
        def __init__(self, name, identifier, labels, status):
            self.name = name
            self.id = identifier
            self.status = status
            self.attrs = {"Config": {"Labels": labels}}
            self.written_file = identifier == "new"

        def reload(self):
            pass

        def stop(self):
            operations.append(f"stop-{self.id}")
            self.status = "exited"

        def start(self):
            operations.append(f"start-{self.id}")
            if self.id == "old" and not old_starts:
                raise RuntimeError("old cannot start")
            self.status = "running"

        def rename(self, name):
            operations.append(f"rename-{self.id}")
            self.name = name

        def remove(self, force=False):
            operations.append(f"remove-{self.id}")
            items.remove(self)

    old = FakeContainer("labgpu-tester-merge-old", "old", {"compute-graveyard.managed": "true", "compute-graveyard.username": "tester", "compute-graveyard.gpu_ids": "0"}, "exited")
    new = FakeContainer("labgpu-tester-merge-new", "new", {"compute-graveyard.merge_source": "old"}, "running")
    items = [old, new]

    def get(identifier):
        for item in items:
            if identifier in {item.id, item.name}:
                return item
        raise NotFound("missing")

    monkeypatch.setattr(docker_service, "get_docker_client", lambda: SimpleNamespace(containers=SimpleNamespace(get=get)))
    with pytest.raises(RuntimeError):
        docker_service.rollback_gpu_merge("old", "labgpu-tester", "tester", [0])
    assert "stop-new" in operations
    assert operations.index("stop-new") < operations.index("start-old")
    assert "remove-new" not in operations
    assert new in items and new.written_file
    if old_starts:
        assert old.status == "running"
        assert new.status == "exited"
    else:
        assert new.status == "running"
        assert "start-new" in operations


def test_agent_merge_timeout_does_not_change_regular_requests(monkeypatch):
    timeouts = []

    def request(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"status": "ok"})

    monkeypatch.setattr(remote_agent.httpx, "request", request)
    client = remote_agent.RemoteAgentClient("http://worker.example", "token", timeout=12)
    client.inventory()
    client.merge_container("old", {})
    client.rollback_merge("old", {})
    client.finalize_merge("old", {})
    assert timeouts == [12] + [remote_agent.AGENT_MERGE_TIMEOUT_SECONDS] * 3
    assert remote_agent.AGENT_MERGE_TIMEOUT_SECONDS > 12


def test_periodic_merge_recovery_retries_after_node_returns(state, monkeypatch, caplog):
    db, user, other, target = state
    target.status = "merging"
    target.gpu_ids = "0,1"
    target.pending_share_json = json.dumps({"old_id": "old", "old_gpus": [0]})
    db.commit()
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(containers, "_merge_action", lambda *args: (_ for _ in ()).throw(RuntimeError("worker down")))
    scheduler._recover_pending_merges()
    db.refresh(target)
    assert target.status == "merging"
    assert "合并恢复失败" in caplog.text
    monkeypatch.setattr(containers, "_merge_action", lambda *args: None)
    scheduler._recover_pending_merges()
    db.refresh(target)
    assert target.status == "running"
    assert target.gpu_ids == "0"
    assert target.pending_share_json is None


def test_expiry_does_not_stop_unfinished_merge(state, monkeypatch):
    db, user, other, target = state
    target.pending_share_json = json.dumps({"old_id": "backup", "old_gpus": [0]})
    target.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    stopped = []
    monkeypatch.setattr(scheduler, "_stop_container_runtime", lambda *args: stopped.append(args))
    scheduler._stop_expired_containers()
    assert stopped == []


def test_pending_failure_does_not_consume_final_approval(state, monkeypatch):
    db, user, other, target = state
    db.add(ContainerModel(container_id="other", name="other", user_id=other.id, node_id="local", gpu_ids="1", ssh_port=22002, status="running", expires_at=datetime.now() + timedelta(days=2)))
    db.commit()
    pending_id = containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db).container.id
    monkeypatch.setattr(containers, "_merge_action", lambda *args: (_ for _ in ()).throw(RuntimeError("offline")) if args[1] == "merge" else None)
    with pytest.raises(HTTPException):
        containers.approve_share(pending_id, user=other, db=db)
    pending = db.get(ContainerModel, pending_id)
    assert pending.status == "pending_share_approval"
    assert not json.loads(pending.pending_share_json)["approvers"][0]["approved"]
    monkeypatch.setattr(containers, "_merge_action", lambda node, action, old_id, data: {"container_id": "new"} if action == "merge" else None)
    containers.approve_share(pending_id, user=other, db=db)
    assert pending.status == "removed"


def test_finalize_failure_stays_merging_and_recovery_closes_pending(state, monkeypatch):
    db, user, other, target = state
    db.add(ContainerModel(container_id="other", name="other", user_id=other.id, node_id="local", gpu_ids="1", ssh_port=22002, status="running", expires_at=datetime.now() + timedelta(days=2)))
    db.commit()
    pending_id = containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db).container.id

    def action(node, operation, old_id, data):
        if operation == "merge":
            return {"container_id": "new"}
        if operation == "finalize":
            raise RuntimeError("offline")

    monkeypatch.setattr(containers, "_merge_action", action)
    with pytest.raises(HTTPException) as exc:
        containers.approve_share(pending_id, user=other, db=db)
    assert exc.value.status_code == 503
    db.refresh(target)
    assert target.status == "merging"
    assert db.get(ContainerModel, pending_id).status == "removed"
    monkeypatch.setattr(containers, "_merge_action", lambda *args: None)
    containers.recover_incomplete_merges(db)
    db.refresh(target)
    assert target.status == "running"
    assert target.pending_share_json is None


def test_merge_reserves_new_gpu_before_runtime_change(state, monkeypatch):
    db, user, other, target = state

    def action(node, operation, old_id, data):
        if operation == "merge":
            db.refresh(target)
            assert target.status == "merging"
            assert target.gpu_ids == "0,1"
            assert containers._distinct_users_per_gpu_map(db, "local")[1] == {user.id}
            return {"container_id": "new"}

    monkeypatch.setattr(containers, "_merge_action", action)
    containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db)


def test_merging_occupancy_counts_for_quota_and_dashboard(state):
    db, user, other, target = state
    target.status = "merging"
    db.commit()
    assert containers._distinct_users_per_gpu_map(db, "local")[0] == {user.id}
    assert dashboard._distinct_users_per_gpu(db, "local")[0] == {user.id}
    from app.node_service import _users_per_gpu
    assert _users_per_gpu(db, "local")[0] == {user.id}


def test_admin_cannot_force_stop_or_remove_merging_target(state):
    db, user, other, target = state
    target.status = "merging"
    db.commit()
    for action in (admin.force_stop, admin.force_remove):
        with pytest.raises(HTTPException) as exc:
            action(target.id, admin=other, db=db)
        assert exc.value.status_code in {409, 500}
    assert target.container_id == "old"


def test_agent_merge_validates_input_and_requires_finalize_id():
    valid = {"name": "labgpu-tester", "username": "tester", "old_gpu_ids": [0], "gpu_ids": [0, 1], "ssh_port": 22001, "extra_ports": {8080: 30001}, "ssh_password_hash": "a" * 64, "mem_limit_gb": 64}
    agent.AgentMergeRequest(**valid)
    for change in ({"name": "../../bad"}, {"username": "../bad"}, {"gpu_ids": [True]}, {"old_gpu_ids": [-1]}, {"extra_ports": {8080: 70000}}, {"ssh_password_hash": "plaintext"}):
        with pytest.raises(ValidationError):
            agent.AgentMergeRequest(**{**valid, **change})
    with pytest.raises(ValidationError):
        agent.AgentMergeFinalize(name="labgpu-tester", username="tester", old_gpu_ids=[0])


def test_migration_adds_nullable_target_id():
    engine = create_engine("sqlite:///:memory:")
    Table("containers", MetaData(), Column("id", Integer, primary_key=True)).create(engine)
    _migrate_container_merge(engine)
    _migrate_container_merge(engine)
    assert "target_container_id" in {column["name"] for column in inspect(engine).get_columns("containers")}
    engine.dispose()
