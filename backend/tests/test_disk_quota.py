from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text

from app import quota_service
from app.database import _migrate_container_stop_reason, _migrate_disk_quota
from app.scheduler import _enforce_disk_quotas, _remove_stopped_containers, _stop_disk_quota_containers
from app.quota_service import QuotaStatus


class FakeDb:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.commits = 0
        self.closed = False

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
        lambda *args: removed.append(args) or SimpleNamespace(success=True, error=None),
    )

    _remove_stopped_containers()

    assert len(removed) == 1
    assert db.closed
