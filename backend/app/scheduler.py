"""定时任务：到期通知、自动回收"""
from contextlib import nullcontext
from datetime import datetime, timedelta
import logging
from typing import Iterable, Mapping, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import DISK_QUOTA_GRACE_HOURS, DISK_QUOTA_SCAN_INTERVAL_MINUTES, NOTIFY_WEBHOOK, NODE_ID, NODE_ROLE
from app.container_lifecycle import merge_cleanup_pending, remove_container_record
from app.database import SessionLocal
from app.database_models import ContainerModel, SystemSettings, UserModel, UserNotificationModel
from app.docker_service import stop_container
from app.node_service import get_node, inventory_for_node, stop_on_node
from app.remote_agent import RemoteAgentError
from app.quota_service import refresh_user_quota
from app.settings_service import idle_policy_signature, load_settings
from app.worker_share import worker_gpu_lock

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
        and not merge_cleanup_pending(container)
        and container.container_id == snapshot["container_id"]
        and (getattr(container, "node_id", None) or NODE_ID) == snapshot.get("node_id", getattr(container, "node_id", None) or NODE_ID)
        and container.gpu_ids == snapshot["gpu_ids"]
        and container.gpu_idle_low_since == snapshot["low_since"]
        and container.gpu_idle_low_since is not None
        and now - container.gpu_idle_low_since >= timedelta(hours=current_settings.idle_gpu_duration_hours)
    )
    if not unchanged:
        _clear_container_idle_window(db, container)
        return None, None
    return container, current_settings


def _worker_lifecycle_lock(container):
    if NODE_ROLE == "worker" and (not getattr(container, "node_id", None) or container.node_id == NODE_ID):
        return worker_gpu_lock
    return nullcontext()


def _stop_container_runtime(db, container: ContainerModel) -> bool:
    if not getattr(container, "node_id", None) or container.node_id == NODE_ID:
        with _worker_lifecycle_lock(container):
            return stop_container(container.container_id)
    return stop_on_node(db, container)


def _mark_stopped_if_running(db, container_id: int, reason: str, now: datetime) -> Optional[ContainerModel]:
    container = db.query(ContainerModel).filter(
        ContainerModel.id == container_id,
        ContainerModel.status == "running",
    ).first()
    if not container or merge_cleanup_pending(container):
        return None
    container.status = "stopped"
    container.stop_reason = reason
    container.stopped_at = now
    container.gpu_idle_low_since = None
    container.gpu_idle_last_sample_at = None
    if reason == "disk_quota":
        db.add(UserNotificationModel(
            user_id=container.user_id, event_key=f"disk_quota_stopped-{container.id}",
            type="disk_quota_stopped", title="容器因磁盘配额被停止",
            message=f"容器 {container.name} 因工作区持续超额已停止。",
            container_id=container.id, container_name=container.name, created_at=now,
        ))
    db.commit()
    return container


def _stop_disk_quota_containers(db, user: UserModel, now: datetime) -> None:
    containers = db.query(ContainerModel).filter(
        ContainerModel.user_id == user.id,
        ContainerModel.status == "running",
    ).all()
    for container in containers:
        if merge_cleanup_pending(container):
            continue
        if not container.container_id:
            logger.error("磁盘配额容器 %s 缺少 Docker ID，无法停止", container.name)
            continue
        try:
            stopped = _stop_container_runtime(db, container)
            if not stopped:
                logger.error("停止磁盘配额容器 %s 失败", container.name)
                continue
            marked = _mark_stopped_if_running(db, container.id, "disk_quota", now)
            if not marked:
                continue
            _send_notify(f"【Lab-GPU】用户 {user.username} 的容器 {marked.name} 因工作区超过磁盘配额 {DISK_QUOTA_GRACE_HOURS} 小时，已停止。")
        except Exception as exc:
            db.rollback()
            logger.exception("停止磁盘配额容器 %s 失败: %s", container.name, exc)


def _enforce_disk_quotas():
    db = SessionLocal()
    try:
        now = datetime.now()
        grace_period = timedelta(hours=DISK_QUOTA_GRACE_HOURS)
        users = db.query(UserModel).filter(UserModel.role == "user").all()
        for user in users:
            try:
                status = refresh_user_quota(db, user, now=now)
                if not status.scan_complete:
                    continue
                if not status.over_quota or not status.exceeded_since:
                    continue
                if now - status.exceeded_since < grace_period:
                    continue
                _stop_disk_quota_containers(db, user, now)
            except Exception as exc:
                db.rollback()
                logger.exception("处理用户 %s 磁盘配额失败: %s", getattr(user, "username", getattr(user, "id", "unknown")), exc)
    except Exception as exc:
        logger.exception("磁盘配额检查失败: %s", exc)
    finally:
        db.close()


_check_disk_quotas = _enforce_disk_quotas


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
            if not merge_cleanup_pending(c) and c.expires_at and c.expires_at <= now and c.container_id:
                if _stop_container_runtime(db, c):
                    marked = _mark_stopped_if_running(db, c.id, "expired", now)
                    if marked:
                        _send_notify(f"【Lab-GPU】容器 {marked.name} 已到期，已执行停止。")
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
            with _worker_lifecycle_lock(container):
                options = {"notification_type": "disk_quota_destroyed"} if getattr(container, "stop_reason", None) == "disk_quota" else {}
                result = remove_container_record(db, container, "停止 24 小时后自动清理", **options)
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

        now = datetime.now()
        duration = timedelta(hours=settings.idle_gpu_duration_hours)
        inventories: dict[str, dict | None] = {}
        containers = db.query(ContainerModel).filter(
            ContainerModel.status == "running",
            ContainerModel.container_id.isnot(None),
            ContainerModel.container_id != "",
            ContainerModel.gpu_ids.isnot(None),
            ContainerModel.gpu_ids != "",
        ).all()

        for container in containers:
            try:
                if merge_cleanup_pending(container):
                    container.gpu_idle_low_since = None
                    container.gpu_idle_last_sample_at = None
                    db.commit()
                    continue
                node_id = container.node_id or NODE_ID
                if node_id not in inventories:
                    node = get_node(db, node_id)
                    if not node or not node.enabled:
                        inventories[node_id] = None
                    else:
                        try:
                            inventories[node_id] = inventory_for_node(db, node)
                        except (RemoteAgentError, RuntimeError, ValueError) as exc:
                            logger.warning("GPU 指标采集失败，节点 %s 本轮不会自动回收: %s", node_id, exc)
                            inventories[node_id] = None
                inventory = inventories[node_id]
                if not inventory:
                    container.gpu_idle_low_since = None
                    container.gpu_idle_last_sample_at = None
                    db.commit()
                    continue
                metrics_by_index = {int(row["index"]): row for row in inventory.get("gpus", [])}
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
                    "node_id": node_id,
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
                with _worker_lifecycle_lock(current):
                    result = remove_container_record(
                        db,
                        current,
                        reason,
                        now=now,
                        expected_status="running",
                        expected_container_id=snapshot["container_id"],
                        expected_gpu_ids=snapshot["gpu_ids"],
                        expected_low_since=snapshot["low_since"],
                        notification_type="gpu_idle_reclaimed",
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


def _recover_pending_merges():
    from app.api.containers import _share_action_lock, recover_incomplete_merges

    with _share_action_lock:
        db = None
        try:
            db = SessionLocal()
            recover_incomplete_merges(db)
        except Exception:
            logger.exception("定时恢复 GPU 合并失败")
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    logger.exception("GPU 合并恢复回滚数据库会话失败")
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    logger.exception("GPU 合并恢复关闭数据库会话失败")


def _reconcile_pending_remote_shares():
    if NODE_ROLE != "master":
        return
    from app.api.containers import _reconcile_remote_shares, _share_action_lock

    with _share_action_lock:
        db = None
        try:
            db = SessionLocal()
            user_ids = [user_id for (user_id,) in db.query(ContainerModel.user_id).filter(
                ContainerModel.status.in_(["pending_share_approval", "provisioning", "share_uncertain"]),
                ContainerModel.pending_share_json.like('%"request_id"%'),
            ).distinct().all()]
            for user_id in user_ids:
                try:
                    _reconcile_remote_shares(db, user_id)
                except Exception:
                    db.rollback()
                    logger.warning("Worker 共用申请用户 %s 暂不可调和", user_id)
        except Exception:
            if db is not None:
                db.rollback()
            logger.warning("Worker 共用申请定时扫描失败")
        finally:
            if db is not None:
                db.close()


def start_scheduler():
    if scheduler.running:
        return
    scheduler.add_job(_check_expiry_and_notify, IntervalTrigger(minutes=30), id="notify", max_instances=1, coalesce=True)
    scheduler.add_job(_stop_expired_containers, IntervalTrigger(minutes=5), id="stop", max_instances=1, coalesce=True)
    scheduler.add_job(_remove_stopped_containers, IntervalTrigger(hours=1), id="remove", max_instances=1, coalesce=True)
    scheduler.add_job(_enforce_disk_quotas, IntervalTrigger(minutes=DISK_QUOTA_SCAN_INTERVAL_MINUTES), id="disk-quota", max_instances=1, coalesce=True)
    scheduler.add_job(_reclaim_idle_gpu_containers, IntervalTrigger(minutes=5), id="idle-gpu-reclaim", max_instances=1, coalesce=True)
    scheduler.add_job(_recover_pending_merges, IntervalTrigger(minutes=5), id="recover-gpu-merges", max_instances=1, coalesce=True)
    if NODE_ROLE == "master":
        scheduler.add_job(_reconcile_pending_remote_shares, IntervalTrigger(minutes=1), id="reconcile-remote-shares", max_instances=1, coalesce=True)
    scheduler.start()
    logger.info("定时任务已启动")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("定时任务已停止")
