from datetime import datetime
from types import SimpleNamespace

from app.container_lifecycle import RemovalResult, remove_container_record


class FakeDb:
    def __init__(self):
        self.commits = 0
        self.on_refresh = None

    def commit(self):
        self.commits += 1

    def refresh(self, container):
        if self.on_refresh:
            self.on_refresh(container)


def make_container():
    return SimpleNamespace(
        id=12,
        name="container-name",
        status="running",
        container_id="docker-id",
        stopped_at=None,
        removed_at=None,
        removal_reason=None,
        gpu_ids="0",
        gpu_idle_low_since=datetime(2026, 1, 1),
        gpu_idle_last_sample_at=datetime(2026, 1, 1),
        pending_share_json=None,
    )


def test_docker_failure_does_not_change_database_record():
    db = FakeDb()
    container = make_container()
    result = remove_container_record(
        db,
        container,
        "test",
        docker_remover=lambda _: RemovalResult(success=False, error="daemon unavailable"),
    )
    assert not result.success
    assert container.status == "running"
    assert container.container_id == "docker-id"
    assert container.name == "container-name"
    assert db.commits == 0


def test_success_marks_removed_and_is_idempotent():
    db = FakeDb()
    container = make_container()
    now = datetime(2026, 1, 2)
    result = remove_container_record(
        db,
        container,
        "idle reclaim",
        now=now,
        docker_remover=lambda _: RemovalResult(success=True),
    )
    assert result.success
    assert container.status == "removed"
    assert container.container_id is None
    assert container.removed_at == now
    assert container.removal_reason == "idle reclaim"
    assert db.commits == 1

    result = remove_container_record(db, container, "again")
    assert result.success
    assert result.already_removed
    assert db.commits == 1


def test_missing_container_id_is_not_assumed_removed():
    db = FakeDb()
    container = make_container()
    container.container_id = None
    result = remove_container_record(db, container, "test")
    assert not result.success
    assert container.status == "running"
    assert db.commits == 0


def test_expected_state_change_skips_docker_removal():
    db = FakeDb()
    container = make_container()
    db.on_refresh = lambda value: setattr(value, "status", "stopped")
    calls = []
    result = remove_container_record(
        db,
        container,
        "test",
        expected_status="running",
        expected_container_id="docker-id",
        expected_gpu_ids="0",
        expected_low_since=datetime(2026, 1, 1),
        docker_remover=lambda value: calls.append(value) or RemovalResult(success=True),
    )
    assert not result.success
    assert calls == []
    assert db.commits == 0
