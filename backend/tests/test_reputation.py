"""Reputation is private, transactional, idempotent and applied across lease paths."""
# ruff: noqa: F811 - pytest fixture imports are consumed through test parameters
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.api import admin, auth, containers, leases
from app.auth import get_current_user
from app.config import MAX_LEASE_DAYS, MAX_REPUTATION_SCORE
from app.container_lifecycle import RemovalResult, remove_container_record
from app.database import Base, get_db, _migrate_reputation
from app.database_models import ReputationEventModel, UserModel
from app.models import ContainerApplyRequest, LeaseRenewRequest, UserRegister
from app.reputation_service import record_event, record_configured_event
from app.settings_service import load_settings, save_settings, idle_policy_signature
from test_gpu_merge import state  # noqa: F401,F811 - shared pytest fixture
from test_worker_share import master_share, user_headers  # noqa: F401


def test_existing_ledger_metadata_migration_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE reputation_events (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO reputation_events (id) VALUES (1)"))
    _migrate_reputation(engine)
    _migrate_reputation(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT reason, source FROM reputation_events")).one() == ("", "custom")
    engine.dispose()


@pytest.mark.parametrize("kind", ["idle_warning", "idle_reclaim", "idle_shrink", "expiry_reward"])
def test_configured_events_have_private_chinese_metadata(state, kind):
    db, user, _, target = state
    event = record_configured_event(db, user.id, kind, kind, target.id)
    db.commit()
    assert event.source == "scheduler"
    assert event.reason and any("\u4e00" <= char <= "\u9fff" for char in event.reason)
    custom = record_event(db, user.id, "plugin", "extension", reason="扩展事件", source="plugin")
    db.commit()
    assert custom.source == "plugin" and custom.reason == "扩展事件"


@pytest.mark.parametrize("field", ["reputation_initial_score", "reputation_idle_warning_points",
    "reputation_idle_reclaim_points", "reputation_idle_shrink_points", "reputation_expiry_reward_points",
    "reputation_tier1_threshold", "reputation_tier2_threshold"])
def test_settings_reject_integer_overflow(state, field):
    db, _, _, _ = state
    with pytest.raises(ValueError):
        save_settings(db, replace(load_settings(db), **{field: MAX_REPUTATION_SCORE + 1}))


def test_score_limit_saturates_with_actual_delta(state):
    db, user, _, _ = state
    admin.update_reputation_score(user.id, admin.ReputationScoreUpdate(reputation_score=MAX_REPUTATION_SCORE - 1), admin=user, db=db)
    event = record_event(db, user.id, "overflow", "custom", delta=2)
    db.commit()
    assert user.reputation_score == MAX_REPUTATION_SCORE
    assert event.delta == 1 and event.score_after == MAX_REPUTATION_SCORE
    capped = record_configured_event(db, user.id, "already-capped", "idle_reclaim")
    db.commit()
    assert capped.delta == 0
    with pytest.raises(HTTPException) as exc:
        admin.update_reputation_score(user.id, admin.ReputationScoreUpdate(reputation_score=MAX_REPUTATION_SCORE + 1), admin=user, db=db)
    assert exc.value.status_code == 400
    for kwargs in ({"score": MAX_REPUTATION_SCORE + 1}, {"delta": MAX_REPUTATION_SCORE + 1},
                   {"delta": -MAX_REPUTATION_SCORE - 1}):
        with pytest.raises(ValueError):
            record_event(db, user.id, "invalid", "custom", **kwargs)
    assert db.query(ReputationEventModel).count() == 3


@pytest.mark.parametrize("flag,status,stop_reason,hours,expected", [
    (False, "stopped", "expired", 25, 5),
    (True, "running", "expired", 25, 5),
    (True, "stopped", "admin", 25, 5),
    (True, "stopped", "expired", 23, 5),
    (True, "stopped", "expired", 24, 3),
])
def test_auto_expiry_reward_requires_explicit_flag_and_verified_state(state, flag, status, stop_reason, hours, expected):
    db, user, _, target = state
    now = datetime.now()
    record_event(db, user.id, "seed", "custom", score=5)
    target.status, target.stop_reason = status, stop_reason
    target.stopped_at = now - timedelta(hours=hours)
    db.commit()
    result = remove_container_record(db, target, "停止 24 小时后自动清理" if not flag else "任意新文案",
        now=now, auto_expiry_cleanup=flag, docker_remover=lambda _: RemovalResult(True))
    assert result.success
    assert user.reputation_score == expected


def test_remote_delayed_approval_rechecks_policy(master_share):
    client, db, _, remote = master_share
    response = client.post("/api/containers/apply", json={"placement_mode": "specific", "node_id": "worker-1",
        "gpu_ids": [0], "lease_days": 4}, headers=user_headers("applicant"))
    assert response.status_code == 200
    user = db.get(UserModel, 1)
    record_event(db, user.id, "penalty", "custom", score=11)
    db.commit()
    remote["state"] = "approved"
    containers._reconcile_remote_shares(db, user.id)
    assert remote["calls"] == 0
    record_event(db, user.id, "reset", "custom", score=0)
    db.commit()
    containers._reconcile_remote_shares(db, user.id)
    assert remote["calls"] == 1


def test_delete_user_removes_ledger(state, monkeypatch):
    db, user, other, target = state
    record_event(db, other.id, "other-event", "custom", delta=1)
    db.commit()
    admin.delete_user(other.id, admin=user, db=db)
    assert db.get(UserModel, other.id) is None
    assert db.query(ReputationEventModel).count() == 0


def test_atomic_idempotency_and_rollback(state):
    db, user, _, target = state
    record_event(db, user.id, "unique", "custom", delta=4)
    db.commit()
    record_event(db, user.id, "unique", "custom", delta=99)
    db.commit()
    assert user.reputation_score == 4
    record_event(db, user.id, "rollback", "custom", delta=9)
    db.rollback()
    assert user.reputation_score == 4
    assert db.query(ReputationEventModel).count() == 1
    record_event(db, user.id, "rollback", "custom", delta=-10)
    db.commit()
    assert user.reputation_score == 0
    assert db.query(ReputationEventModel).filter_by(event_key="rollback").one().delta == -4


def test_concurrent_events_do_not_lose_updates(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scores.db'}", connect_args={"timeout": 20})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="concurrent", hashed_password="x")
        db.add(user)
        db.commit()
        uid = user.id
    def apply(i):
        with Session(engine) as db:
            record_event(db, uid, f"key-{i % 4}", "custom", delta=1)
            db.commit()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(apply, range(16)))
    with Session(engine) as db:
        assert db.get(UserModel, uid).reputation_score == 4
        assert db.query(ReputationEventModel).count() == 4
    engine.dispose()


def test_migration_old_users_zero():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO users (id) VALUES (1)"))
    _migrate_reputation(engine)
    _migrate_reputation(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT reputation_score FROM users")).scalar() == 0


@pytest.mark.parametrize("score,maximum", [(0, MAX_LEASE_DAYS), (5, MAX_LEASE_DAYS), (6, 5), (10, 5), (11, 3)])
def test_policy_strict_thresholds(state, score, maximum):
    db, user, _, _ = state
    record_event(db, user.id, "set", "custom", score=score)
    db.commit()
    assert containers.application_policy(user=user, db=db) == {"gpu_max_lease_days": maximum, "cpu_max_lease_days": MAX_LEASE_DAYS}


@pytest.mark.parametrize("field,value", [("reputation_initial_score", -1), ("reputation_initial_score", True),
    ("reputation_idle_warning_points", 1.5), ("reputation_tier2_threshold", 5),
    ("reputation_tier1_max_days", MAX_LEASE_DAYS + 1), ("reputation_tier2_max_days", 6)])
def test_settings_validation(state, field, value):
    db, _, _, _ = state
    with pytest.raises(ValueError):
        save_settings(db, replace(load_settings(db), **{field: value}))


@pytest.mark.parametrize("value", [True, 1.5, "2"])
def test_admin_strict_integers(value):
    with pytest.raises(ValidationError):
        admin.ReputationScoreUpdate(reputation_score=value)
    with pytest.raises(ValidationError):
        admin.SettingsUpdate(cpu_mem_gb=1, gpu_mem_gb_per_gpu=1, max_gpu_sharing_users=1,
            idle_gpu_reclaim_enabled=True, idle_gpu_util_threshold_percent=1,
            idle_gpu_memory_threshold_percent=1, idle_gpu_duration_hours=1, reputation_initial_score=value)


def test_legacy_settings_preserve_reputation_and_observation(state):
    db, user, _, _ = state
    settings = load_settings(db)
    save_settings(db, replace(settings, reputation_idle_warning_points=7, reputation_initial_score=4))
    assert idle_policy_signature(settings) == idle_policy_signature(load_settings(db))
    legacy = {k: v for k, v in asdict(settings).items() if not k.startswith("reputation_")}
    admin.update_settings(admin.SettingsUpdate(**legacy), admin=user, db=db)
    assert load_settings(db).reputation_idle_warning_points == 7
    assert load_settings(db).reputation_initial_score == 4
    assert user.reputation_score == 0


def test_new_users_only_apply_initial_score(state, monkeypatch):
    db, user, _, _ = state
    save_settings(db, replace(load_settings(db), reputation_initial_score=8))
    monkeypatch.setattr(admin, "get_password_hash", lambda _: "x")
    monkeypatch.setattr(auth, "get_password_hash", lambda _: "x")
    admin.create_user(admin.UserCreate(username="created", password="password"), admin=user, db=db)
    auth.register(UserRegister(username="zhangsan", password="password", real_name="张三", contact_type="phone", contact_value="123"), db=db)
    assert db.query(UserModel).filter_by(username="created").one().reputation_score == 8
    assert db.query(UserModel).filter_by(username="zhangsan").one().reputation_score == 8
    assert user.reputation_score == 0


def test_new_apply_approval_merge_and_cpu(state, monkeypatch):
    db, user, _, target = state
    record_event(db, user.id, "set", "custom", score=11)
    db.commit()
    monkeypatch.setattr(containers, "provision_on_node", lambda *args: {"container_id": "cpu", "ssh_port": 23000})
    with pytest.raises(HTTPException) as exc:
        containers.apply_container(ContainerApplyRequest(gpu_ids=[1], lease_days=4), user=user, db=db)
    assert exc.value.detail == "租期须在 1~3 天之间"
    assert containers.apply_container(ContainerApplyRequest(cpu_only=True, lease_days=MAX_LEASE_DAYS), user=user, db=db).container.gpu_ids == ""
    with pytest.raises(HTTPException):
        containers._provision_running_container(db, target, 4)
    target.expires_at = datetime.now() + timedelta(days=4)
    db.commit()
    with pytest.raises(HTTPException):
        containers.apply_container(ContainerApplyRequest(target_container_id=target.id, gpu_ids=[1]), user=user, db=db)
    with pytest.raises(HTTPException):
        containers._perform_merge(db, target, [1], user)
    assert target.expires_at > datetime.now() + timedelta(days=3)


def test_renew_checks_current_policy_every_time(state):
    db, user, _, target = state
    target.expires_at = datetime.now() + timedelta(hours=1)
    db.commit()
    leases.renew_lease(target.id, LeaseRenewRequest(lease_days=3), user=user, db=db)
    target.expires_at = datetime.now() + timedelta(hours=1)
    record_event(db, user.id, "set", "custom", score=11)
    db.commit()
    with pytest.raises(HTTPException):
        leases.renew_lease(target.id, LeaseRenewRequest(lease_days=4), user=user, db=db)
    leases.renew_lease(target.id, LeaseRenewRequest(lease_days=3), user=user, db=db)


@pytest.mark.parametrize("mode,expected", [("idle", 7), ("expiry", 3), ("admin", 5), ("disk", 5), ("failed", 5)])
def test_removal_scoring_only_after_success(state, mode, expected):
    db, user, _, target = state
    record_event(db, user.id, "set", "custom", score=5)
    target.status = "stopped" if mode in ("expiry", "disk") else "running"
    target.stop_reason = "expired" if mode == "expiry" else "disk_quota"
    target.stopped_at = datetime.now() - timedelta(hours=25)
    db.commit()
    reason = "停止 24 小时后自动清理" if mode in ("expiry", "disk") else "管理员强制清理"
    options = {"notification_type": "gpu_idle_reclaimed"} if mode == "idle" else {}
    if mode in ("expiry", "disk"):
        options["auto_expiry_cleanup"] = True
    result = remove_container_record(db, target, reason, docker_remover=lambda _: RemovalResult(mode != "failed"), **options)
    assert user.reputation_score == expected
    if result.success:
        remove_container_record(db, target, reason, **options)
        assert user.reputation_score == expected


def test_expiry_beats_idle_and_shrink_is_idempotent(state):
    db, user, _, target = state
    target.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    result = remove_container_record(db, target, "idle", notification_type="gpu_idle_reclaimed",
        docker_remover=lambda _: pytest.fail("expired container must not be reclaimed"))
    assert not result.success
    assert user.reputation_score == 0
    target.gpu_ids = "0,1"
    containers._finish_idle_shrink(db, target, [1], "old")
    db.commit()
    containers._finish_idle_shrink(db, target, [1], "old")
    db.commit()
    assert user.reputation_score == 2
    assert db.query(ReputationEventModel).count() == 1


def test_admin_audit_reset_and_user_privacy(state):
    db, user, _, target = state
    response = admin.update_reputation_score(user.id, admin.ReputationScoreUpdate(reputation_score=7), admin=user, db=db)
    assert response["reputation_score"] == 7
    admin.update_reputation_score(user.id, admin.ReputationScoreUpdate(reputation_score=0), admin=user, db=db)
    events = admin.reputation_events(user.id, admin=user, db=db)
    assert events[0]["score_before"] == 7 and events[0]["score_after"] == 0
    assert events[0]["actor_id"] == user.id
    assert events[0]["source"] == "admin"
    assert events[0]["reason"] == "管理员清零信誉分"
    assert events[1]["reason"] == "管理员手动设置信誉分"
    assert "reputation" not in str(auth.me(user=user).model_dump())
    # The real admin dependency must reject normal users, without any score payload.
    app = FastAPI()
    app.include_router(admin.router, prefix="/admin")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    for method, path, payload in [("get", f"/admin/users/{user.id}/reputation-events", None),
                                 ("put", f"/admin/users/{user.id}/reputation-score", {"reputation_score": 1})]:
        response = client.request(method, path, json=payload)
        assert response.status_code == 403
        assert "reputation_score" not in response.text
