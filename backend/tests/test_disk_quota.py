from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import containers as containers_api
from app.auth import create_access_token
from app.container_lifecycle import RemovalResult, remove_container_record
from app.database import Base, get_db
from app.database_models import ComputeNodeModel, ContainerModel, UserModel, UserNotificationModel

from app import quota_service
from app.database import _migrate_container_stop_reason, _migrate_disk_quota
from app.scheduler import _enforce_disk_quotas, _mark_stopped_if_running, _remove_stopped_containers, _stop_disk_quota_containers, _stop_expired_containers
from app.quota_service import QuotaStatus


class FakeDb:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.commits = 0
        self.closed = False
        self.events = []

    def add(self, event):
        self.events.append(event)

    def query(self, model):
        return FakeQuery(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args):
        return self

    def all(self):
        return self.rows

    def first(self):
        for row in self.rows:
            if getattr(row, "status", None) == "running":
                return row
        return self.rows[0] if self.rows else None


class FakeSessionFactory:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self.db


def test_workspace_scan_counts_files_without_following_symlinks(tmp_path):
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "one.bin").write_bytes(b"1234")
    (nested / "two.bin").write_bytes(b"56789")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (root / "file-link").symlink_to(outside)
    (root / "dir-link").symlink_to(nested, target_is_directory=True)

    assert quota_service.calculate_workspace_usage(root) == 9


def test_workspace_scan_missing_root_is_empty(tmp_path):
    assert quota_service.calculate_workspace_usage(tmp_path / "missing") == 0


def test_workspace_scan_deduplicates_hard_links(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    original = root / "original.bin"
    original.write_bytes(b"12345")
    (root / "hard-link.bin").hardlink_to(original)

    assert quota_service.calculate_workspace_usage(root) == 5


def test_missing_storage_root_is_incomplete(monkeypatch, tmp_path):
    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path / "missing-storage")

    result = quota_service.workspace_usage_for_user_result("alice")

    assert result.usage_bytes == 0
    assert not result.complete


def test_worker_master_workspace_scan_is_read_only_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path)
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "local.bin").write_bytes(b"local")
    assert quota_service.worker_master_workspace_usage_result("alice") == quota_service.WorkspaceUsage(0, True, False)
    assert not (tmp_path / ".compute-graveyard-master").exists()

    namespace = tmp_path / ".compute-graveyard-master"
    namespace.mkdir(mode=0o700)
    (namespace / ".namespace").touch(mode=0o600)
    (namespace / "alice").mkdir()
    (namespace / "alice" / "remote.bin").write_bytes(b"remote")
    assert quota_service.worker_master_workspace_usage_result("alice") == quota_service.WorkspaceUsage(6, True)
    assert quota_service.worker_master_workspace_usage_result("bob") == quota_service.WorkspaceUsage(0, True)
    (namespace / "alice" / "link").symlink_to(tmp_path / "alice")
    assert quota_service.worker_master_workspace_usage_result("alice") == quota_service.WorkspaceUsage(6, True)
    (namespace / ".namespace").unlink()
    assert not quota_service.worker_master_workspace_usage_result("alice").complete
    (namespace / ".namespace").symlink_to(tmp_path / "alice" / "local.bin")
    assert not quota_service.worker_master_workspace_usage_result("alice").complete
    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path / "missing")
    assert not quota_service.worker_master_workspace_usage_result("alice").complete


def test_worker_workspace_data_reports_retained_entries_and_unsafe_namespace(monkeypatch, tmp_path):
    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path)
    assert quota_service.worker_master_workspace_data_result() == (False, True)
    namespace = tmp_path / ".compute-graveyard-master"
    namespace.mkdir(mode=0o700)
    (namespace / ".namespace").touch(mode=0o600)
    assert quota_service.worker_master_workspace_data_result() == (False, True)
    (namespace / "alice").mkdir()
    assert quota_service.worker_master_workspace_data_result() == (True, True)
    (namespace / "alice").rmdir()
    (namespace / "link").symlink_to(tmp_path)
    assert quota_service.worker_master_workspace_data_result() == (True, True)
    (namespace / "link").unlink()
    (namespace / ".namespace").unlink()
    assert quota_service.worker_master_workspace_data_result() == (True, False)
    (namespace / ".namespace").symlink_to(tmp_path)
    assert quota_service.worker_master_workspace_data_result() == (True, False)
    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path / "missing")
    assert quota_service.worker_master_workspace_data_result() == (True, False)


def test_agent_workspace_usage_requires_worker_token_and_valid_username(monkeypatch, tmp_path):
    from app.api import agent

    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path)
    monkeypatch.setattr(agent, "NODE_ROLE", "worker")
    monkeypatch.setattr(agent, "AGENT_API_TOKEN", "test-token")
    app = FastAPI()
    app.include_router(agent.router, prefix="/api/agent/v1")
    with TestClient(app) as client:
        path = "/api/agent/v1/workspace-usage/alice"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"node_id": agent.NODE_ID, "username": "alice", "usage_bytes": 0, "complete": True, "namespace_present": False}
        assert client.get("/api/agent/v1/workspace-usage/ALICE", headers=headers).status_code == 422
        data_path = "/api/agent/v1/workspace-data"
        assert client.get(data_path).status_code == 401
        assert client.get(data_path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get(data_path, headers=headers).json() == {
            "node_id": agent.NODE_ID, "has_workspace_data": False, "complete": True,
        }
        monkeypatch.setattr(agent, "NODE_ROLE", "master")
        assert client.get(path, headers=headers).status_code == 404
        assert client.get(data_path, headers=headers).status_code == 404


def test_master_aggregates_all_workers_and_preserves_state_on_failure(monkeypatch, tmp_path):
    from app.remote_agent import RemoteAgentError

    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path)
    monkeypatch.setattr(quota_service, "NODE_ROLE", "master")
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "local.bin").write_bytes(b"1234")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="x", role="user", disk_quota_bytes=10)
        db.add(user)
        db.add_all([
            ComputeNodeModel(id="worker-a", name="a", base_url="http://worker-a.example", agent_token="token", enabled=False),
            ComputeNodeModel(id="worker-b", name="b", base_url="http://worker-b.example", agent_token="token", schedulable=False),
        ])
        db.commit()
        calls = []

        def usage(client, username):
            calls.append((client.base_url, username))
            return {"node_id": "worker-a" if "worker-a" in client.base_url else "worker-b",
                    "username": username, "usage_bytes": 3, "complete": True, "namespace_present": True}

        monkeypatch.setattr(quota_service.RemoteAgentClient, "workspace_usage", usage)
        now = datetime(2026, 1, 1)
        status = quota_service.refresh_user_quota(db, user, now=now)
        assert status.usage_bytes == 10 and status.over_quota
        assert len(calls) == 2
        assert user.disk_quota_exceeded_since == now
        checked_at = user.disk_usage_checked_at

        for change in (
            {"node_id": "wrong"}, {"username": "bob"}, {"usage_bytes": True},
            {"usage_bytes": -1}, {"complete": 1}, {"complete": False},
            {"namespace_present": 1}, {"namespace_present": None},
        ):
            def malformed(client, username):
                return {**usage(client, username), **change} if "worker-a" in client.base_url else usage(client, username)
            monkeypatch.setattr(quota_service.RemoteAgentClient, "workspace_usage", malformed)
            incomplete = quota_service.refresh_user_quota(db, user, now=now + timedelta(hours=25))
            assert not incomplete.scan_complete and incomplete.blocked
            assert user.disk_usage_bytes == 10
            assert user.disk_quota_exceeded_since == now
            assert user.disk_usage_checked_at == checked_at
        monkeypatch.setattr(quota_service.RemoteAgentClient, "workspace_usage", lambda *_: None)
        assert not quota_service.refresh_user_quota(db, user).scan_complete
        def unavailable(*_):
            raise RemoteAgentError("offline")
        monkeypatch.setattr(quota_service.RemoteAgentClient, "workspace_usage", unavailable)
        assert not quota_service.refresh_user_quota(db, user).scan_complete
        quota_service.refresh_user_quota(db, user, usage_bytes=0)
        assert user.disk_usage_bytes == 0 and user.disk_usage_scan_complete
    engine.dispose()


def test_missing_worker_namespace_requires_no_prior_user_container(monkeypatch, tmp_path):
    monkeypatch.setattr(quota_service, "NODE_ROLE", "master")
    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="x", role="user", disk_quota_bytes=100)
        node = ComputeNodeModel(id="remote", name="remote", base_url="http://remote.example", agent_token="token")
        db.add_all([user, node])
        db.commit()
        monkeypatch.setattr(quota_service.RemoteAgentClient, "workspace_usage", lambda _client, name: {
            "node_id": "remote", "username": name, "usage_bytes": 0, "complete": True,
            "namespace_present": False,
        })
        assert quota_service.refresh_user_quota(db, user).scan_complete
        db.add(ContainerModel(name="old-box", user_id=user.id, node_id=node.id, ssh_port=22001,
                              status="removed", expires_at=datetime.now() + timedelta(days=1)))
        db.commit()
        assert not quota_service.refresh_user_quota(db, user).scan_complete
        assert not quota_service.check_user_can_provision(db, user).allowed
        db.query(ContainerModel).delete()
        db.commit()
        assert quota_service.refresh_user_quota(db, user).scan_complete
    engine.dispose()


def test_master_remote_usage_stops_remote_container_after_grace(monkeypatch, tmp_path):
    from app import scheduler

    monkeypatch.setattr(quota_service, "NODE_ROLE", "master")
    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path)
    monkeypatch.setattr(scheduler, "NODE_ROLE", "master")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="x", role="user", disk_quota_bytes=10)
        node = ComputeNodeModel(id="remote", name="remote", base_url="http://remote.example", agent_token="token")
        db.add_all([user, node])
        db.flush()
        container = ContainerModel(name="remote-box", user_id=user.id, node_id=node.id,
                                   container_id="docker-remote", ssh_port=20001, status="running",
                                   expires_at=datetime.now() + timedelta(days=2))
        db.add(container)
        db.commit()
        user_id, container_id = user.id, container.id
        monkeypatch.setattr(quota_service.RemoteAgentClient, "workspace_usage", lambda _client, name: {
            "node_id": "remote", "username": name, "usage_bytes": 11, "complete": True, "namespace_present": True,
        })
        stopped = []
        monkeypatch.setattr(scheduler, "stop_on_node", lambda _db, row: stopped.append(row.container_id) or True)
        monkeypatch.setattr(scheduler, "_send_notify", lambda *_: None)
        monkeypatch.setattr(scheduler, "SessionLocal", lambda: Session(engine))
        scheduler._enforce_disk_quotas()
        assert stopped == []
        with Session(engine) as check:
            owner = check.get(UserModel, user_id)
            owner.disk_quota_exceeded_since = datetime.now() - timedelta(hours=25)
            check.commit()
        scheduler._enforce_disk_quotas()
        assert stopped == ["docker-remote"]
        with Session(engine) as check:
            row = check.get(ContainerModel, container_id)
            assert row.status == "stopped" and row.stop_reason == "disk_quota"
    engine.dispose()


def test_worker_local_quota_never_queries_master_workspaces(monkeypatch, tmp_path):
    monkeypatch.setattr(quota_service, "NODE_ROLE", "worker")
    monkeypatch.setattr(quota_service, "USER_DATA_BASE", tmp_path)
    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "local.bin").write_bytes(b"123")
    monkeypatch.setattr(quota_service.RemoteAgentClient, "workspace_usage", lambda *_: pytest.fail("remote scan"))
    user = SimpleNamespace(username="alice", role="user", disk_quota_bytes=10, disk_usage_bytes=0)
    assert quota_service.refresh_user_quota(FakeDb(), user).usage_bytes == 3


def test_refresh_user_quota_blocks_and_clears(monkeypatch):
    now = datetime(2026, 1, 1)
    user = SimpleNamespace(
        id=1,
        username="alice",
        role="user",
        disk_quota_bytes=100,
        disk_usage_bytes=0,
        disk_quota_blocked=False,
        disk_quota_exceeded_since=None,
    )
    db = FakeDb()
    monkeypatch.setattr(
        quota_service,
        "workspace_usage_for_user_result",
        lambda _: quota_service.WorkspaceUsage(101, True),
    )

    status = quota_service.refresh_user_quota(db, user, now=now)
    assert status.blocked
    assert user.disk_usage_bytes == 101
    assert user.disk_quota_exceeded_since == now

    monkeypatch.setattr(
        quota_service,
        "workspace_usage_for_user_result",
        lambda _: quota_service.WorkspaceUsage(99, True),
    )
    status = quota_service.refresh_user_quota(db, user, now=now + timedelta(hours=1))
    assert not status.blocked
    assert user.disk_quota_exceeded_since is None


def test_exact_quota_is_still_blocked_until_usage_is_below_limit(monkeypatch):
    user = SimpleNamespace(
        username="alice",
        role="user",
        disk_quota_bytes=100,
        disk_usage_bytes=0,
        disk_quota_blocked=False,
        disk_quota_exceeded_since=None,
    )
    monkeypatch.setattr(
        quota_service,
        "workspace_usage_for_user_result",
        lambda _: quota_service.WorkspaceUsage(100, True),
    )

    status = quota_service.refresh_user_quota(FakeDb(), user, now=datetime(2026, 1, 1))

    assert status.blocked
    assert status.over_quota


def test_incomplete_scan_fails_closed_without_resetting_state(monkeypatch):
    exceeded_since = datetime(2026, 1, 1)
    user = SimpleNamespace(
        username="alice",
        role="user",
        disk_quota_bytes=100,
        disk_usage_bytes=120,
        disk_quota_blocked=False,
        disk_quota_exceeded_since=exceeded_since,
        disk_usage_scan_complete=True,
    )
    monkeypatch.setattr(
        quota_service,
        "workspace_usage_for_user_result",
        lambda _: quota_service.WorkspaceUsage(0, False),
    )

    status = quota_service.refresh_user_quota(FakeDb(), user)

    assert not status.scan_complete
    assert not status.allowed
    assert status.blocked
    assert user.disk_usage_bytes == 120
    assert user.disk_quota_exceeded_since == exceeded_since


def test_admin_quota_is_exempt(monkeypatch):
    user = SimpleNamespace(
        id=1,
        username="admin",
        role="admin",
        disk_quota_bytes=1,
        disk_usage_bytes=999,
        disk_quota_blocked=True,
        disk_quota_exceeded_since=datetime(2026, 1, 1),
    )
    monkeypatch.setattr(quota_service, "workspace_usage_for_user", lambda _: 9999)

    status = quota_service.refresh_user_quota(FakeDb(), user)
    assert status.exempt
    assert status.allowed
    assert not status.over_quota
    assert not user.disk_quota_blocked


def test_disk_quota_migrations_are_idempotent_and_backfill_defaults():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    users = Table("users", metadata, Column("id", Integer, primary_key=True), Column("username", String(64)))
    Table("containers", metadata, Column("id", Integer, primary_key=True), Column("name", String(128)))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(users.insert().values(id=1, username="alice"))

    _migrate_disk_quota(engine)
    _migrate_disk_quota(engine)
    _migrate_container_stop_reason(engine)
    _migrate_container_stop_reason(engine)

    user_columns = {column["name"] for column in inspect(engine).get_columns("users")}
    container_columns = {column["name"] for column in inspect(engine).get_columns("containers")}
    assert {
        "disk_quota_bytes",
        "disk_usage_bytes",
        "disk_usage_checked_at",
        "disk_usage_scan_complete",
        "disk_quota_exceeded_since",
        "disk_quota_blocked",
    }.issubset(user_columns)
    assert "stop_reason" in container_columns
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT disk_quota_bytes, disk_usage_bytes, disk_usage_scan_complete, disk_quota_blocked FROM users WHERE id = 1")
        ).one()
    assert row[0] == 100 * 1024**3
    assert row[1] == 0
    assert row[2] in (False, 0)
    assert row[3] in (False, 0)


def test_disk_quota_enforcement_waits_until_grace_period(monkeypatch):
    user = SimpleNamespace(id=1, username="alice", role="user")
    db = FakeDb([user])
    monkeypatch.setattr("app.scheduler.SessionLocal", FakeSessionFactory(db))
    monkeypatch.setattr(
        "app.scheduler.refresh_user_quota",
        lambda *_args, **_kwargs: QuotaStatus(
            usage_bytes=101,
            quota_bytes=100,
            over_quota=True,
            blocked=True,
            exceeded_since=datetime.now() - timedelta(hours=25),
            exempt=False,
        ),
    )
    stopped = []
    monkeypatch.setattr("app.scheduler._stop_disk_quota_containers", lambda *args: stopped.append(args))

    _enforce_disk_quotas()

    assert len(stopped) == 1
    assert db.closed


def test_disk_quota_stop_marks_all_running_containers(monkeypatch):
    monkeypatch.setattr("app.scheduler.reject_shares_for_exit", lambda *args, **kwargs: None)
    now = datetime(2026, 1, 2)
    containers = [
        SimpleNamespace(
            id=1,
            name="one",
            container_id="docker-one",
            user_id=1,
            status="running",
            stop_reason=None,
            stopped_at=None,
            gpu_idle_low_since=now,
            gpu_idle_last_sample_at=now,
        ),
        SimpleNamespace(
            id=2,
            name="two",
            container_id="docker-two",
            user_id=1,
            status="running",
            stop_reason=None,
            stopped_at=None,
            gpu_idle_low_since=now,
            gpu_idle_last_sample_at=now,
        ),
    ]
    db = FakeDb(containers)
    user = SimpleNamespace(id=1, username="alice")
    monkeypatch.setattr("app.scheduler.stop_container", lambda _: True)

    _stop_disk_quota_containers(db, user, now)

    assert all(container.status == "stopped" for container in containers)
    assert all(container.stop_reason == "disk_quota" for container in containers)
    assert all(container.stopped_at == now for container in containers)
    assert db.commits == 2


def test_worker_scheduler_expiry_and_cleanup_hold_gpu_lock(monkeypatch):
    monkeypatch.setattr("app.scheduler.reject_shares_for_exit", lambda *args, **kwargs: None)
    from app import scheduler

    class Lock:
        held = False

        def __enter__(self):
            assert not self.held
            self.held = True

        def __exit__(self, *args):
            self.held = False

    lock = Lock()
    monkeypatch.setattr(scheduler, "NODE_ROLE", "worker")
    monkeypatch.setattr(scheduler, "worker_gpu_lock", lock)
    now = datetime.now()
    container = SimpleNamespace(id=1, name="expired", node_id=scheduler.NODE_ID, container_id="docker-one",
                                status="running", expires_at=now - timedelta(hours=1), gpu_ids="0",
                                pending_share_json=None, stop_reason=None, stopped_at=None,
                                gpu_idle_low_since=None, gpu_idle_last_sample_at=None)
    db = FakeDb([container])
    monkeypatch.setattr(scheduler, "SessionLocal", FakeSessionFactory(db))
    monkeypatch.setattr(scheduler, "stop_container", lambda _: lock.held)
    monkeypatch.setattr(scheduler, "_send_notify", lambda *_: None)
    _stop_expired_containers()
    assert container.status == "stopped"
    container.stopped_at = now - timedelta(hours=25)
    monkeypatch.setattr(scheduler, "remove_container_record", lambda *args: SimpleNamespace(success=lock.held, error=None))
    _remove_stopped_containers()
    assert not lock.held


def test_generic_cleanup_handles_disk_quota_containers(monkeypatch):
    container = SimpleNamespace(
        name="quota-stopped",
        status="stopped",
        stop_reason="disk_quota",
        stopped_at=datetime.now() - timedelta(hours=48),
    )
    db = FakeDb([container])
    monkeypatch.setattr("app.scheduler.SessionLocal", FakeSessionFactory(db))
    removed = []
    monkeypatch.setattr(
        "app.scheduler.remove_container_record",
        lambda *args, **kwargs: removed.append((args, kwargs)) or SimpleNamespace(success=True, error=None),
    )

    _remove_stopped_containers()

    assert len(removed) == 1
    assert removed[0][1]["notification_type"] == "disk_quota_destroyed"
    assert db.closed


def test_disk_events_only_on_complete_committed_band_crossings(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="x", role="user", disk_quota_bytes=100)
        admin = UserModel(username="boss", hashed_password="x", role="admin", disk_quota_bytes=100)
        db.add_all([user, admin])
        db.commit()
        for usage in (100, 100, 95, 89, 100):
            quota_service.refresh_user_quota(db, user, usage_bytes=usage)
        assert [e.type for e in db.query(UserNotificationModel).order_by(UserNotificationModel.id)] == [
            "disk_usage_90", "disk_usage_100", "disk_usage_90", "disk_usage_100",
        ]
        monkeypatch.setattr(quota_service, "workspace_usage_for_user_result", lambda _: quota_service.WorkspaceUsage(0, False))
        quota_service.refresh_user_quota(db, user)
        assert user.disk_notification_band == 100
        quota_service.refresh_user_quota(db, admin, usage_bytes=1000)
        assert db.query(UserNotificationModel).count() == 4
        quota_service.refresh_user_quota(db, user, usage_bytes=0, commit=False)
        db.rollback()
        assert db.get(UserModel, user.id).disk_notification_band == 100
        assert db.query(UserNotificationModel).count() == 4
    engine.dispose()


def test_uncommitted_quota_crossings_commit_or_rollback_together():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="x", role="user", disk_quota_bytes=100)
        db.add(user)
        db.commit()
        user_id = user.id

        quota_service.refresh_user_quota(db, user, usage_bytes=100, commit=False)
        assert user.disk_notification_band == 100
        db.rollback()
        assert db.get(UserModel, user_id).disk_notification_band == 0
        assert db.query(UserNotificationModel).count() == 0

        quota_service.refresh_user_quota(db, user, usage_bytes=100, commit=False)
        db.commit()
        db.expire_all()
        assert db.get(UserModel, user_id).disk_notification_band == 100
        assert [event.type for event in db.query(UserNotificationModel).order_by(UserNotificationModel.id)] == [
            "disk_usage_90", "disk_usage_100",
        ]
    engine.dispose()


def test_disk_stop_and_destroy_are_distinct_and_retry_safe():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="x")
        db.add(user)
        db.commit()
        now = datetime.now()
        container = ContainerModel(name="disk-box", user_id=user.id, container_id="docker-id", ssh_port=20002,
                                   expires_at=now + timedelta(days=1), status="running")
        db.add(container)
        db.commit()
        assert _mark_stopped_if_running(db, container.id, "disk_quota", now)
        assert _mark_stopped_if_running(db, container.id, "disk_quota", now) is None
        assert remove_container_record(db, container, "cleanup", docker_remover=lambda _: RemovalResult(success=False),
                                       notification_type="disk_quota_destroyed").success is False
        assert [e.type for e in db.query(UserNotificationModel).all()] == ["disk_quota_stopped"]
        assert remove_container_record(db, container, "cleanup", docker_remover=lambda _: RemovalResult(success=True),
                                       notification_type="disk_quota_destroyed").success
        assert remove_container_record(db, container, "cleanup", notification_type="disk_quota_destroyed").already_removed
        assert [e.type for e in db.query(UserNotificationModel).order_by(UserNotificationModel.id)] == [
            "disk_quota_stopped", "disk_quota_destroyed",
        ]
    engine.dispose()


def test_notification_security_read_state_and_lifecycle():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        alice = UserModel(username="alice", hashed_password="x", role="user")
        bob = UserModel(username="bob", hashed_password="x", role="user")
        db.add_all([alice, bob])
        db.commit()
        now = datetime.now()
        container = ContainerModel(name="original", user_id=alice.id, container_id="docker-id", ssh_port=20001,
                                   expires_at=now + timedelta(days=1), status="running", gpu_ids="0")
        db.add(container)
        db.commit()
        failed = remove_container_record(db, container, "idle", docker_remover=lambda _: RemovalResult(success=False),
                                         notification_type="gpu_idle_reclaimed")
        assert not failed.success
        assert db.query(UserNotificationModel).count() == 0
        succeeded = remove_container_record(db, container, "idle", docker_remover=lambda _: RemovalResult(success=True),
                                            notification_type="gpu_idle_reclaimed")
        assert succeeded.success
        assert remove_container_record(db, container, "idle", notification_type="gpu_idle_reclaimed").already_removed
        event = db.query(UserNotificationModel).one()
        assert event.container_name == "original"
        app = FastAPI()
        app.include_router(containers_api.router, prefix="/api/containers")
        app.dependency_overrides[get_db] = lambda: db
        alice_headers = {"Authorization": f"Bearer {create_access_token({'sub': 'alice'})}"}
        bob_headers = {"Authorization": f"Bearer {create_access_token({'sub': 'bob'})}"}
        with TestClient(app) as client:
            path = f"/api/containers/notifications/{event.id}/read"
            assert client.get("/api/containers/notifications").status_code in (401, 403)
            assert client.get("/api/containers/notifications", headers=bob_headers).json()["items"] == []
            assert client.post(path, headers=bob_headers).status_code == 404
            assert client.get("/api/containers/notifications", headers=alice_headers).json()["unread_count"] == 1
            assert client.post(path, headers=alice_headers).status_code == 200
            assert client.post(path, headers=alice_headers).status_code == 200
            assert client.get("/api/containers/notifications", headers=alice_headers).json()["unread_count"] == 0
    engine.dispose()
