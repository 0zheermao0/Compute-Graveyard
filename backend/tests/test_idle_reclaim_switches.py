from dataclasses import asdict, replace
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.admin import SettingsUpdate, update_settings
from app.database_models import SystemSettings
from app.scheduler import _candidate_still_reclaimable, gpu_set_is_low, _sync_policy_signature
from app.settings_service import SettingsValues, idle_policy_signature, load_settings
from test_scheduler_recheck import FakeDb, container


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    SystemSettings.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def settings(**overrides):
    return replace(SettingsValues(8, 32, 4, True, 5, 5, 1), **overrides)


@pytest.mark.parametrize("dual_low,memory_unchanged", [(True, True), (True, False), (False, True), (False, False)])
def test_independent_idle_criteria(dual_low, memory_unchanged):
    metrics = {
        0: {"utilization": 2, "memory_percent": 1},
        1: {"utilization": 0, "memory_percent": 80, "memory_used_mb": 16000},
    }
    previous = {"1": 16000}
    options = {"dual_low_enabled": dual_low, "memory_unchanged_enabled": memory_unchanged}
    assert gpu_set_is_low([0], metrics, 5, 5, previous, **options) == dual_low
    assert gpu_set_is_low([1], metrics, 5, 5, previous, **options) == memory_unchanged
    assert gpu_set_is_low([0, 1], metrics, 5, 5, previous, **options) == (dual_low and memory_unchanged)
    assert not gpu_set_is_low([], metrics, 5, 5, previous, **options)


def test_legacy_settings_and_api_default_switches_to_true(db):
    legacy = asdict(settings())
    legacy.pop("idle_gpu_dual_low_enabled")
    legacy.pop("idle_gpu_memory_unchanged_enabled")
    for key, value in legacy.items():
        db.add(SystemSettings(key=key, value=str(value)))
    db.commit()
    loaded = load_settings(db)
    assert loaded.idle_gpu_dual_low_enabled is True
    assert loaded.idle_gpu_memory_unchanged_enabled is True
    request = SettingsUpdate(**legacy)
    saved = update_settings(request, admin=None, db=db)
    assert saved == settings()
    assert load_settings(db) == saved


@pytest.mark.parametrize("dual_low,memory_unchanged", [(True, True), (True, False), (False, True), (False, False)])
def test_switches_persist_independently(db, dual_low, memory_unchanged):
    values = settings(idle_gpu_dual_low_enabled=dual_low, idle_gpu_memory_unchanged_enabled=memory_unchanged)
    request = SettingsUpdate(**asdict(values))
    assert update_settings(request, admin=None, db=db) == values
    assert load_settings(db) == values
    rows = {row.key: row.value for row in db.query(SystemSettings).all()}
    assert rows["idle_gpu_dual_low_enabled"] == str(dual_low).lower()
    assert rows["idle_gpu_memory_unchanged_enabled"] == str(memory_unchanged).lower()


@pytest.mark.parametrize("field", ["idle_gpu_dual_low_enabled", "idle_gpu_memory_unchanged_enabled"])
def test_switch_change_changes_signature_and_resets_candidate(monkeypatch, field):
    original = settings()
    changed = replace(original, **{field: False})
    assert idle_policy_signature(changed) != idle_policy_signature(original)
    now = datetime(2026, 1, 1, 2)
    value = container(now)
    db = FakeDb(value)
    snapshot = {"container_id": value.container_id, "gpu_ids": value.gpu_ids, "low_since": value.gpu_idle_low_since}
    monkeypatch.setattr("app.scheduler.load_settings", lambda _: changed)
    assert _candidate_still_reclaimable(db, value.id, snapshot, idle_policy_signature(original), now) == (None, None)
    assert value.gpu_idle_low_since is None
    assert value.gpu_idle_stage_mask == 0
    assert value.gpu_idle_warned_at is None
    assert db.commits == 1


@pytest.mark.parametrize("field", ["idle_gpu_dual_low_enabled", "idle_gpu_memory_unchanged_enabled"])
def test_switch_change_resets_sampling_policy(db, monkeypatch, field):
    resets = []
    monkeypatch.setattr("app.scheduler._clear_idle_windows", lambda _: resets.append(True))
    original = idle_policy_signature(settings())
    assert _sync_policy_signature(db, original)
    assert not _sync_policy_signature(db, original)
    changed = idle_policy_signature(settings(**{field: False}))
    assert _sync_policy_signature(db, changed)
    assert len(resets) == 2
