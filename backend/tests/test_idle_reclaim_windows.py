"""Exercise the complete idle-reclaim job against real, persisted SQLite models.

Only time, node discovery/inventory, runtime deletion and external notifications
are mocked; policy loading, stage tracking, final checks and lifecycle writes run
unchanged.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler
from app.database import Base
from app.database_models import ContainerModel, SystemSettings, UserModel, UserNotificationModel


START = datetime(2026, 1, 1)


def inventory(utilization=1, memory_percent=1, memory_used_mb=100):
    return {"gpus": [
        {"index": index, "utilization": utilization,
         "memory_percent": memory_percent, "memory_used_mb": memory_used_mb}
        for index in (0, 1)
    ]}


@pytest.fixture
def reclaim(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        user = UserModel(username="idle-owner", hashed_password="unused")
        db.add(user)
        db.flush()
        row = ContainerModel(
            name="idle-container", container_id="docker-id", user_id=user.id,
            node_id="test-node", gpu_ids="0,1", ssh_port=22001,
            status="running", expires_at=START + timedelta(days=10),
        )
        db.add(row)
        db.add_all([
            SystemSettings(key="idle_gpu_reclaim_enabled", value="true"),
            SystemSettings(key="idle_gpu_duration_hours", value="1"),
            SystemSettings(key="idle_gpu_util_threshold_percent", value="5"),
            SystemSettings(key="idle_gpu_memory_threshold_percent", value="5"),
        ])
        db.commit()
        row_id = row.id

    clock = SimpleNamespace(now=START)

    class Clock:
        @classmethod
        def now(cls):
            return clock.now

    node = SimpleNamespace(id="test-node", enabled=True)
    collect = Mock(return_value=inventory())
    delete = Mock(return_value=True)
    notify = Mock()
    discover = Mock(return_value=node)
    monkeypatch.setattr(scheduler, "SessionLocal", sessions)
    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(scheduler, "get_node", discover)
    monkeypatch.setattr(scheduler, "inventory_for_node", collect)
    monkeypatch.setattr(scheduler, "_send_notify", notify)
    monkeypatch.setattr("app.node_service.delete_on_node", delete)

    class Harness:
        def duration(self, hours):
            with sessions() as db:
                db.query(SystemSettings).filter_by(key="idle_gpu_duration_hours").one().value = str(hours)
                db.commit()

        def tick(self, minute, metrics=None):
            clock.now = START + timedelta(minutes=minute)
            if metrics is not None:
                collect.return_value = metrics
            scheduler._reclaim_idle_gpu_containers()

        def sample_until(self, end, start=0, metrics=None):
            for minute in range(start, end + 1, 5):
                self.tick(minute, metrics)

        def row(self):
            with sessions() as db:
                return db.get(ContainerModel, row_id)

        def update(self, **values):
            with sessions() as db:
                row = db.get(ContainerModel, row_id)
                for key, value in values.items():
                    setattr(row, key, value)
                db.commit()

        def notifications(self, kind):
            with sessions() as db:
                return db.query(UserNotificationModel).filter_by(type=kind).order_by(UserNotificationModel.id).all()

        def assert_no_removal(self):
            delete.assert_not_called()
            assert self.row().status == "running"
            assert not self.notifications("gpu_idle_reclaimed")
            assert not any("已立即停止并销毁" in call.args[0] for call in notify.call_args_list)

    harness = Harness()
    harness.collect = collect
    harness.delete = delete
    harness.notify = notify
    harness.discover = discover
    harness.node = node
    yield harness
    engine.dispose()


@pytest.mark.parametrize("hours", [1, 24])
@pytest.mark.parametrize("resident", [False, True], ids=["dual-low", "zero-util-stable-memory"])
def test_continuous_five_minute_samples_warn_once_then_recheck_and_reclaim(reclaim, hours, resident):
    reclaim.duration(hours)
    metrics = inventory(0, 75, 7500) if resident else inventory()
    # A resident-memory observation needs a previous sample, not an inferred one.
    if resident:
        reclaim.tick(-5, metrics)
        assert reclaim.row().gpu_idle_low_since is None
    end = hours * 60
    warning_minute = next(minute for minute in range(0, end + 1, 5) if minute >= end * 0.8)
    reclaim.sample_until(warning_minute - 5, metrics=metrics)
    assert not reclaim.notifications("gpu_idle_warning")
    reclaim.assert_no_removal()

    reclaim.tick(warning_minute, metrics)
    warnings = reclaim.notifications("gpu_idle_warning")
    assert len(warnings) == 1
    assert warnings[0].created_at == START + timedelta(minutes=warning_minute)
    assert warnings[0].container_name == "idle-container"
    assert reclaim.row().gpu_idle_stage_mask == 31
    assert reclaim.row().gpu_idle_warned_at == warnings[0].created_at
    reclaim.sample_until(end - 5, start=warning_minute + 5, metrics=metrics)
    assert len(reclaim.notifications("gpu_idle_warning")) == 1
    reclaim.notify.assert_called_once()
    reclaim.assert_no_removal()

    reclaim.collect.reset_mock()
    reclaim.discover.reset_mock()
    reclaim.tick(end, metrics)
    # One round sample plus an independent last-moment inventory, both on this node.
    assert reclaim.collect.call_count == 2
    assert reclaim.discover.call_count == 2
    assert all(call.args[1] is reclaim.node for call in reclaim.collect.call_args_list)
    reclaim.delete.assert_called_once()
    row = reclaim.row()
    assert row.status == "removed"
    assert row.container_id is None
    assert row.removed_at == START + timedelta(minutes=end)
    assert row.gpu_idle_low_since is None
    assert row.gpu_idle_warned_at is None
    assert row.gpu_idle_stage_mask == 0
    assert row.gpu_idle_memory_snapshot is None
    successes = reclaim.notifications("gpu_idle_reclaimed")
    assert len(successes) == 1
    assert successes[0].container_name == "idle-container"
    assert successes[0].created_at == row.removed_at
    assert reclaim.notify.call_count == 2
    assert "已立即停止并销毁" in reclaim.notify.call_args.args[0]
    reclaim.tick(end + 5)
    assert len(reclaim.notifications("gpu_idle_reclaimed")) == 1
    reclaim.delete.assert_called_once()


def test_one_assigned_gpu_recovers_and_resets_all_stage_and_warning_state(reclaim):
    reclaim.sample_until(50)
    assert reclaim.row().gpu_idle_warned_at is not None
    metrics = inventory()
    metrics["gpus"][1]["utilization"] = 90
    reclaim.tick(55, metrics)
    row = reclaim.row()
    assert row.gpu_idle_low_since is None
    assert row.gpu_idle_stage_mask == 0
    assert row.gpu_idle_warned_at is None
    assert row.gpu_idle_last_sample_at == START + timedelta(minutes=55)
    reclaim.tick(60, inventory())
    assert reclaim.row().gpu_idle_low_since == START + timedelta(minutes=60)
    assert reclaim.row().gpu_idle_stage_mask == 1
    reclaim.assert_no_removal()


def test_resident_memory_change_resets_then_requires_new_stable_sample(reclaim):
    stable = inventory(0, 75, 7500)
    reclaim.tick(-5, stable)
    reclaim.sample_until(50, metrics=stable)
    assert len(reclaim.notifications("gpu_idle_warning")) == 1
    changed = inventory(0, 76, 7600)
    reclaim.tick(55, changed)
    row = reclaim.row()
    assert row.gpu_idle_low_since is None
    assert row.gpu_idle_stage_mask == 0
    assert row.gpu_idle_warned_at is None
    reclaim.tick(60, changed)
    assert reclaim.row().gpu_idle_low_since == START + timedelta(minutes=60)
    assert reclaim.row().gpu_idle_stage_mask == 1
    reclaim.assert_no_removal()


@pytest.mark.parametrize("final", ["normal", "memory-change", "failure", "missing-gpu", "disabled-node"])
def test_final_recheck_rejects_recovery_or_unavailable_evidence(reclaim, final):
    resident = final == "memory-change"
    low = inventory(0, 75, 7500) if resident else inventory()
    if resident:
        reclaim.tick(-5, low)
    reclaim.sample_until(55, metrics=low)
    reclaim.collect.reset_mock()
    if final == "disabled-node":
        reclaim.discover.side_effect = [reclaim.node, SimpleNamespace(enabled=False)]
    else:
        final_sample = {
            "normal": inventory(90, 75, 7500),
            "memory-change": inventory(0, 76, 7600),
            "failure": RuntimeError("inventory offline"),
            "missing-gpu": {"gpus": inventory()["gpus"][:1]},
        }[final]
        reclaim.collect.side_effect = [low, final_sample]
    reclaim.tick(60)
    assert reclaim.collect.call_count == (1 if final == "disabled-node" else 2)
    reclaim.assert_no_removal()
    row = reclaim.row()
    assert row.gpu_idle_low_since is None
    assert row.gpu_idle_warned_at is None
    assert row.gpu_idle_stage_mask == 0
    assert row.gpu_idle_memory_snapshot is None
    assert len(reclaim.notifications("gpu_idle_warning")) == 1


@pytest.mark.parametrize("missing_bit", [1, 2, 4, 8])
def test_missing_observation_stage_cannot_be_inferred_at_deadline(reclaim, missing_bit):
    reclaim.sample_until(55)
    reclaim.update(gpu_idle_stage_mask=31 & ~missing_bit)
    reclaim.collect.reset_mock()
    reclaim.tick(60)
    reclaim.assert_no_removal()
    reclaim.collect.assert_called_once()
    row = reclaim.row()
    assert row.gpu_idle_low_since == START + timedelta(hours=1)
    assert row.gpu_idle_stage_mask == 1
    assert row.gpu_idle_warned_at is None


def test_large_sampling_gap_cannot_complete_an_old_cycle(reclaim):
    reclaim.sample_until(50)
    reclaim.tick(70)
    reclaim.assert_no_removal()
    assert reclaim.row().gpu_idle_low_since == START + timedelta(minutes=70)
    assert reclaim.row().gpu_idle_stage_mask == 1
    assert reclaim.row().gpu_idle_warned_at is None


def test_failed_runtime_deletion_never_writes_or_sends_success(reclaim):
    reclaim.sample_until(55)
    reclaim.delete.return_value = False
    reclaim.tick(60)
    reclaim.delete.assert_called_once()
    row = reclaim.row()
    assert row.status == "running"
    assert row.container_id == "docker-id"
    assert row.removed_at is None
    assert not reclaim.notifications("gpu_idle_reclaimed")
    reclaim.notify.assert_called_once()
    assert "已立即停止并销毁" not in reclaim.notify.call_args.args[0]


def test_recovery_after_warning_allows_a_new_cycle_to_warn_again(reclaim):
    reclaim.sample_until(50)
    reclaim.tick(55, inventory(90, 90, 9000))
    reclaim.sample_until(105, start=60, metrics=inventory())
    assert len(reclaim.notifications("gpu_idle_warning")) == 1
    reclaim.tick(110)
    warnings = reclaim.notifications("gpu_idle_warning")
    assert len(warnings) == 2
    assert warnings[0].event_key != warnings[1].event_key
    assert warnings[1].created_at == START + timedelta(minutes=110)
    assert reclaim.row().gpu_idle_low_since == START + timedelta(minutes=60)
    assert reclaim.row().gpu_idle_warned_at == warnings[1].created_at
    assert reclaim.notify.call_count == 2
    reclaim.assert_no_removal()
    reclaim.tick(115)
    assert len(reclaim.notifications("gpu_idle_warning")) == 2
    reclaim.tick(120)
    reclaim.delete.assert_called_once()
    assert len(reclaim.notifications("gpu_idle_reclaimed")) == 1
