"""定时任务：到期通知、自动回收"""
from datetime import datetime, timedelta
import logging
from typing import Iterable, Mapping, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import NOTIFY_WEBHOOK
from app.container_lifecycle import remove_container_record
from app.database import SessionLocal
from app.database_models import ContainerModel, SystemSettings
from app.docker_service import get_gpu_info, stop_container
from app.settings_service import idle_policy_signature, load_settings

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()
IDLE_SAMPLE_MAX_GAP = timedelta(minutes=15)
IDLE_POLICY_SIGNATURE_KEY = "idle_gpu_policy_signature"


def _send_notify(msg: str):
    if not NOTIFY_WEBHOOK:
        logger.info("[通知] %s", msg)
        return
    try:
        import httpx
        httpx.post(
            NOTIFY_WEBHOOK,
            json={"msgtype": "text", "text": {"content": msg}},
            timeout=5,
        )
    except Exception as e:
        logger.warning("Webhook 发送失败: %s", e)


def gpu_set_is_low(
    gpu_ids: Iterable[int],
    metrics_by_index: Mapping[int, Mapping],
    utilization_threshold: int,
    memory_threshold: int,
) -> bool:
    ids = list(gpu_ids)
    if not ids:
        return False
    for gpu_id in ids:
        metric = metrics_by_index.get(gpu_id)
        if not metric:
            return False
        utilization = metric.get("utilization")
        memory_percent = metric.get("memory_percent")
        if utilization is None or memory_percent is None:
            return False
        if utilization >= utilization_threshold or memory_percent >= memory_threshold:
            return False
    return True


def next_idle_window(
    low_since: Optional[datetime],
    last_sample_at: Optional[datetime],
    now: datetime,
    is_low: bool,
    max_gap: timedelta = IDLE_SAMPLE_MAX_GAP,
) -> tuple[Optional[datetime], datetime]:
    if not is_low:
        return None, now
    if low_since is None or last_sample_at is None or now - last_sample_at > max_gap:
        return now, now
    return low_since, now


def _clear_idle_windows(db):
    rows = db.query(ContainerModel).filter(
        ContainerModel.status == "running",
        ContainerModel.gpu_ids.isnot(None),
        ContainerModel.gpu_ids != "",
    ).all()
    for container in rows:
        container.gpu_idle_low_since = None
        container.gpu_idle_last_sample_at = None


def _sync_policy_signature(db, signature: str) -> bool:
    row = db.query(SystemSettings).filter(SystemSettings.key == IDLE_POLICY_SIGNATURE_KEY).first()
    if row and row.value == signature:
        return False
    _clear_idle_windows(db)
    if row:
        row.value = signature
    else:
        db.add(SystemSettings(key=IDLE_POLICY_SIGNATURE_KEY, value=signature))
    db.commit()
    return True


def _clear_container_idle_window(db, container):
    container.gpu_idle_low_since = None
    container.gpu_idle_last_sample_at = None
    db.commit()


def _candidate_still_reclaimable(db, container_id: int, snapshot: dict, signature: str, now: datetime):
    try:
        current_settings = load_settings(db)
    except (TypeError, ValueError) as exc:
        logger.error("销毁前 GPU 自动回收设置无效，跳过容器 %s: %s", container_id, exc)
        container = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
        if container:
            _clear_container_idle_window(db, container)
        return None, None

    container = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
    if not container:
        return None, None
    current_signature = idle_policy_signature(current_settings)
    unchanged = (
        current_settings.idle_gpu_reclaim_enabled
        and current_signature == signature
        and container.status == "running"
        and container.container_id == snapshot["container_id"]
        and container.gpu_ids == snapshot["gpu_ids"]
        and container.gpu_idle_low_since == snapshot["low_since"]
        and container.gpu_idle_low_since is not None
        and now - container.gpu_idle_low_since >= timedelta(hours=current_settings.idle_gpu_duration_hours)
    )
    if not unchanged:
        _clear_container_idle_window(db, container)
        return None, None
    return container, current_settings


def _check_expiry_and_notify():
    db = SessionLocal()
    try:
        now = datetime.now()
        for c in db.query(ContainerModel).filter(ContainerModel.status == "running").all():
            if not c.expires_at:
                continue
            delta = (c.expires_at - now).total_seconds()
            from app.database_models import UserModel
            u = db.query(UserModel).filter(UserModel.id == c.user_id).first()
            owner_name = u.username if u else "未知"
            if 11.5 * 3600 <= delta <= 12.5 * 3600:
                _send_notify(f"【Lab-GPU】容器 {c.name} 将在约 12 小时后到期，请及时续租。用户: {owner_name}")
            elif 1.5 * 3600 <= delta <= 2.5 * 3600:
                _send_notify(f"【Lab-GPU】容器 {c.name} 将在约 2 小时后到期！用户: {owner_name}")
    except Exception as e:
        logger.exception("到期检查失败: %s", e)
    finally:
        db.close()


def _stop_expired_containers():
    db = SessionLocal()
    try:
        now = datetime.now()
        for c in db.query(ContainerModel).filter(ContainerModel.status == "running").all():
            if c.expires_at and c.expires_at <= now and c.container_id:
                if stop_container(c.container_id):
                    c.status = "stopped"
                    c.stopped_at = now
                    c.gpu_idle_low_since = None
                    c.gpu_idle_last_sample_at = None
                    db.commit()
                    _send_notify(f"【Lab-GPU】容器 {c.name} 已到期，已执行停止。")
    except Exception as e:
        logger.exception("停用过期容器失败: %s", e)
    finally:
        db.close()


def _remove_stopped_containers():
    db = SessionLocal()
    try:
        threshold = datetime.now() - timedelta(hours=24)
        containers = db.query(ContainerModel).filter(ContainerModel.status == "stopped").all()
        for container in containers:
            if not container.stopped_at or container.stopped_at > threshold:
                continue
            result = remove_container_record(db, container, "停止 24 小时后自动清理")
            if result.success:
                logger.info("已清理容器 %s", container.name)
            else:
                logger.error("清理容器 %s 失败: %s", container.name, result.error)
    except Exception as e:
        logger.exception("清理已停止容器失败: %s", e)
    finally:
        db.close()


def _reclaim_idle_gpu_containers():
    db = SessionLocal()
    try:
        try:
            settings = load_settings(db)
        except (TypeError, ValueError) as exc:
            logger.error("GPU 低利用自动回收设置无效，本轮关闭回收: %s", exc)
            _clear_idle_windows(db)
            db.commit()
            return

        signature = idle_policy_signature(settings)
        if _sync_policy_signature(db, signature):
            logger.info("GPU 低利用自动回收策略已变化，连续计时已重置")

        if not settings.idle_gpu_reclaim_enabled:
            _clear_idle_windows(db)
            db.commit()
            return

        gpu_rows = get_gpu_info()
        if not gpu_rows:
            _clear_idle_windows(db)
            db.commit()
            logger.warning("GPU 指标采集失败，本轮不会自动回收")
            return

        metrics_by_index = {row["index"]: row for row in gpu_rows}
        now = datetime.now()
        duration = timedelta(hours=settings.idle_gpu_duration_hours)
        containers = db.query(ContainerModel).filter(
            ContainerModel.status == "running",
            ContainerModel.container_id.isnot(None),
            ContainerModel.container_id != "",
            ContainerModel.gpu_ids.isnot(None),
            ContainerModel.gpu_ids != "",
        ).all()

        for container in containers:
            try:
                gpu_ids = [int(value.strip()) for value in container.gpu_ids.split(",") if value.strip()]
                is_low = gpu_set_is_low(
                    gpu_ids,
                    metrics_by_index,
                    settings.idle_gpu_util_threshold_percent,
                    settings.idle_gpu_memory_threshold_percent,
                )
                low_since, last_sample_at = next_idle_window(
                    container.gpu_idle_low_since,
                    container.gpu_idle_last_sample_at,
                    now,
                    is_low,
                )
                container.gpu_idle_low_since = low_since
                container.gpu_idle_last_sample_at = last_sample_at
                db.commit()
                if low_since is None or now - low_since < duration:
                    continue
                snapshot = {
                    "container_id": container.container_id,
                    "gpu_ids": container.gpu_ids,
                    "low_since": low_since,
                }
                db.expire_all()
                current, current_settings = _candidate_still_reclaimable(db, container.id, snapshot, signature, now)
                if not current or not current_settings:
                    continue
                reason = (
                    f"全部分配 GPU 的整卡利用率低于 {current_settings.idle_gpu_util_threshold_percent}% 且显存占用低于 "
                    f"{current_settings.idle_gpu_memory_threshold_percent}%，连续 {current_settings.idle_gpu_duration_hours} 小时"
                )
                original_name = current.name
                result = remove_container_record(
                    db,
                    current,
                    reason,
                    now=now,
                    expected_status="running",
                    expected_container_id=snapshot["container_id"],
                    expected_gpu_ids=snapshot["gpu_ids"],
                    expected_low_since=snapshot["low_since"],
                )
                if result.success:
                    _send_notify(f"【Lab-GPU】容器 {original_name} 因 {reason}，已立即停止并销毁，宿主机 workspace 已保留。")
                else:
                    logger.error("GPU 低利用容器 %s 销毁失败: %s", original_name, result.error)
            except Exception as exc:
                db.rollback()
                logger.exception("处理 GPU 低利用容器 %s 失败: %s", container.id, exc)
    except Exception as e:
        logger.exception("GPU 低利用自动回收检查失败: %s", e)
    finally:
        db.close()


def start_scheduler():
    scheduler.add_job(_check_expiry_and_notify, IntervalTrigger(minutes=30), id="notify", max_instances=1, coalesce=True)
    scheduler.add_job(_stop_expired_containers, IntervalTrigger(minutes=5), id="stop", max_instances=1, coalesce=True)
    scheduler.add_job(_remove_stopped_containers, IntervalTrigger(hours=1), id="remove", max_instances=1, coalesce=True)
    scheduler.add_job(_reclaim_idle_gpu_containers, IntervalTrigger(minutes=5), id="idle-gpu-reclaim", max_instances=1, coalesce=True)
    scheduler.start()
    logger.info("定时任务已启动")
