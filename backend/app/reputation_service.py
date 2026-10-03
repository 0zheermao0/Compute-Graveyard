"""Private admin-only reputation ledger. Callers own the surrounding transaction."""
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.config import MAX_LEASE_DAYS, MAX_REPUTATION_SCORE
from app.database_models import ReputationEventModel, UserModel
from app.settings_service import load_settings


def record_event(db, user_id, event_key, event_type, *, delta=0, score=None,
                 actor_id=None, container_id=None, reason="", source="custom"):
    if (type(delta) is not int or not -MAX_REPUTATION_SCORE <= delta <= MAX_REPUTATION_SCORE
            or (score is not None and (type(score) is not int or not 0 <= score <= MAX_REPUTATION_SCORE))):
        raise ValueError("Invalid reputation adjustment")
    if not isinstance(reason, str) or not isinstance(source, str) or not 1 <= len(source) <= 64:
        raise ValueError("Invalid reputation event metadata")
    # Preserve other pending changes before refreshing the locked user row.
    db.flush()
    # A write lock also serializes SQLite writers; PostgreSQL locks this row.
    db.execute(update(UserModel).where(UserModel.id == user_id).values(
        reputation_score=UserModel.reputation_score).execution_options(synchronize_session=False))
    user = db.query(UserModel).filter(UserModel.id == user_id).populate_existing().first()
    if user is None:
        raise ValueError("User not found")
    existing = db.query(ReputationEventModel).filter_by(event_key=event_key).first()
    if existing:
        return existing
    before = user.reputation_score
    after = min(MAX_REPUTATION_SCORE, max(0, before + delta)) if score is None else score
    event = ReputationEventModel(user_id=user_id, event_key=event_key, event_type=event_type,
        reason=reason, source=source,
        delta=after - before, score_before=before, score_after=after,
        actor_id=actor_id, container_id=container_id)
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
            db.execute(update(UserModel).where(UserModel.id == user_id).values(
                reputation_score=after).execution_options(synchronize_session=False))
    except IntegrityError:
        existing = db.query(ReputationEventModel).filter_by(event_key=event_key).first()
        if existing is None:
            raise
        return existing
    db.expire(user, ["reputation_score"])
    return event


def record_configured_event(db, user_id, key, kind, container_id=None):
    field = {"idle_warning": "reputation_idle_warning_points",
             "idle_reclaim": "reputation_idle_reclaim_points",
             "idle_shrink": "reputation_idle_shrink_points",
             "expiry_reward": "reputation_expiry_reward_points"}[kind]
    points = getattr(load_settings(db), field)
    reason = {"idle_warning": "GPU 长期闲置预警",
              "idle_reclaim": "GPU 长期闲置整容器自动回收",
              "idle_shrink": "长期闲置 GPU 自动缩卡成功",
              "expiry_reward": "容器到期停止满 24 小时后自动销毁奖励"}[kind]
    return record_event(db, user_id, key, kind, reason=reason, source="scheduler",
                        delta=-points if kind == "expiry_reward" else points,
                        container_id=container_id)


def gpu_max_lease_days(db, user):
    settings = load_settings(db)
    score = db.query(UserModel.reputation_score).filter(UserModel.id == user.id).scalar() or 0
    if score > settings.reputation_tier2_threshold:
        return settings.reputation_tier2_max_days
    if score > settings.reputation_tier1_threshold:
        return settings.reputation_tier1_max_days
    return MAX_LEASE_DAYS


def enforce_lease(db, user, days, *, gpu=True, expires_at=None):
    maximum = gpu_max_lease_days(db, user) if gpu else MAX_LEASE_DAYS
    if days < 1 or days > maximum or (expires_at and expires_at > datetime.now() + timedelta(days=maximum)):
        raise HTTPException(status_code=400, detail=f"租期须在 1~{maximum} 天之间")
    return maximum
