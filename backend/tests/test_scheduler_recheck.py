from datetime import datetime, timedelta
from types import SimpleNamespace

from app.scheduler import _candidate_still_reclaimable
from app.settings_service import SettingsValues, idle_policy_signature


class FakeQuery:
    def __init__(self, value):
        self.value = value

    def filter(self, *args):
        return self

    def first(self):
        return self.value


class FakeDb:
    def __init__(self, container):
        self.container = container
        self.commits = 0

    def query(self, model):
        return FakeQuery(self.container)

    def commit(self):
        self.commits += 1


def settings(enabled=True, utilization=5):
    return SettingsValues(
        cpu_mem_gb=8,
        gpu_mem_gb_per_gpu=32,
        max_gpu_sharing_users=4,
        idle_gpu_reclaim_enabled=enabled,
        idle_gpu_util_threshold_percent=utilization,
        idle_gpu_memory_threshold_percent=5,
        idle_gpu_duration_hours=1,
    )


def container(now):
    return SimpleNamespace(
        id=1,
        status="running",
        container_id="docker-id",
        gpu_ids="0",
        gpu_idle_low_since=now - timedelta(hours=2),
        gpu_idle_last_sample_at=now,
    )


def test_policy_change_before_removal_clears_timer(monkeypatch):
    now = datetime(2026, 1, 1, 2)
    value = container(now)
    db = FakeDb(value)
    original = settings()
    monkeypatch.setattr("app.scheduler.load_settings", lambda _: settings(utilization=6))
    current, current_settings = _candidate_still_reclaimable(
        db,
        value.id,
        {"container_id": "docker-id", "gpu_ids": "0", "low_since": value.gpu_idle_low_since},
        idle_policy_signature(original),
        now,
    )
    assert current is None
    assert current_settings is None
    assert value.gpu_idle_low_since is None
    assert db.commits == 1


def test_status_change_before_removal_clears_timer(monkeypatch):
    now = datetime(2026, 1, 1, 2)
    value = container(now)
    value.status = "stopped"
    db = FakeDb(value)
    current_values = settings()
    monkeypatch.setattr("app.scheduler.load_settings", lambda _: current_values)
    current, current_settings = _candidate_still_reclaimable(
        db,
        value.id,
        {"container_id": "docker-id", "gpu_ids": "0", "low_since": value.gpu_idle_low_since},
        idle_policy_signature(current_values),
        now,
    )
    assert current is None
    assert current_settings is None
    assert value.gpu_idle_low_since is None
    assert db.commits == 1
