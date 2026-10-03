import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from docker.errors import DockerException, NotFound

from app.database_models import ContainerModel, UserNotificationModel
from app.docker_service import get_docker_client
from app.share_lifecycle import reject_shares_for_exit, synchronized_occupancy_exit


@dataclass
class RemovalResult:
    success: bool
    already_removed: bool = False
    error: Optional[str] = None


def _remove_from_docker(container_id: str) -> RemovalResult:
    try:
        container = get_docker_client().containers.get(container_id)
    except NotFound:
        return RemovalResult(success=True, already_removed=True)
    except DockerException as exc:
        return RemovalResult(success=False, error=str(exc))

    try:
        container.stop()
    except NotFound:
        return RemovalResult(success=True, already_removed=True)
    except DockerException:
        pass

    try:
        container.remove(force=True)
        return RemovalResult(success=True)
    except NotFound:
        return RemovalResult(success=True, already_removed=True)
    except DockerException as exc:
        return RemovalResult(success=False, error=str(exc))


def merge_cleanup_pending(container: ContainerModel) -> bool:
    if container.status == "merging":
        return True
    try:
        return container.status == "running" and "old_id" in json.loads(getattr(container, "pending_share_json", None) or "{}")
    except (ValueError, TypeError):
        return bool(container.pending_share_json)


@synchronized_occupancy_exit
def remove_container_record(
    db,
    container: ContainerModel,
    reason: str,
    now: Optional[datetime] = None,
    docker_remover: Optional[Callable[[str], RemovalResult]] = None,
    expected_status: Optional[str] = None,
    expected_container_id: Optional[str] = None,
    expected_gpu_ids: Optional[str] = None,
    expected_low_since: Optional[datetime] = None,
    notification_type: Optional[str] = None,
    auto_expiry_cleanup: bool = False,
) -> RemovalResult:
    # Callers may have loaded the record before acquiring the shared GPU lock.
    if hasattr(db, "refresh"):
        db.refresh(container)
    if expected_status is not None and container.status != expected_status:
        return RemovalResult(success=False, error="容器状态已变化")
    if expected_container_id is not None and container.container_id != expected_container_id:
        return RemovalResult(success=False, error="Docker 容器 ID 已变化")
    if expected_gpu_ids is not None and container.gpu_ids != expected_gpu_ids:
        return RemovalResult(success=False, error="容器 GPU 分配已变化")
    if expected_low_since is not None and container.gpu_idle_low_since != expected_low_since:
        return RemovalResult(success=False, error="容器低利用计时已变化")
    if container.status == "removed" and not container.container_id:
        return RemovalResult(success=True, already_removed=True)
    if merge_cleanup_pending(container):
        return RemovalResult(success=False, error="GPU 合并尚未完成，禁止删除容器")

    if notification_type == "gpu_idle_reclaimed" and container.expires_at <= (now or datetime.now()):
        return RemovalResult(success=False, error="容器已到期，等待到期停止清理")
    if not container.container_id:
        return RemovalResult(success=False, error="数据库记录缺少 Docker 容器 ID，无法确认容器已不存在")

    if docker_remover is None:
        from app.node_service import delete_on_node
        result = RemovalResult(success=delete_on_node(db, container))
        if not result.success:
            result.error = "节点容器删除失败"
    else:
        result = docker_remover(container.container_id)
    if not result.success:
        return result

    removed_at = now or datetime.now()
    from app.reputation_service import record_configured_event
    if notification_type == "gpu_idle_reclaimed":
        record_configured_event(db, container.user_id, f"idle-reclaim-{container.id}", "idle_reclaim", container.id)
    elif (auto_expiry_cleanup and container.status == "stopped"
          and container.stop_reason == "expired" and container.stopped_at
          and removed_at >= container.stopped_at + timedelta(hours=24)):
        record_configured_event(db, container.user_id, f"expiry-reward-{container.id}", "expiry_reward", container.id)
    reject_shares_for_exit(db, container, now=removed_at)
    original_name = container.name
    suffix = f"-del-{container.id}-{int(removed_at.timestamp())}"
    container.name = f"{original_name[:max(1, 128 - len(suffix))]}{suffix}"
    container.status = "removed"
    container.stopped_at = container.stopped_at or removed_at
    container.removed_at = removed_at
    container.removal_reason = reason
    container.container_id = None
    container.gpu_idle_low_since = None
    container.gpu_idle_last_sample_at = None
    container.gpu_idle_stage_mask = 0
    container.gpu_idle_warned_at = None
    container.gpu_idle_memory_snapshot = None
    container.gpu_idle_cards_json = None
    container.pending_share_json = None
    if notification_type:
        db.add(UserNotificationModel(
            user_id=container.user_id, event_key=f"{notification_type}-{container.id}",
            type=notification_type,
            title="容器因磁盘配额被销毁" if notification_type == "disk_quota_destroyed" else "GPU 低利用容器已回收",
            message=f"容器 {original_name} 已自动销毁，工作区保留。",
            container_id=container.id, container_name=original_name, created_at=removed_at,
        ))
    db.commit()
    return result
