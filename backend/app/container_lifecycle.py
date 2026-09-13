from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from docker.errors import DockerException, NotFound

from app.database_models import ContainerModel
from app.docker_service import get_docker_client


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
) -> RemovalResult:
    has_expectations = any(
        value is not None
        for value in (expected_status, expected_container_id, expected_gpu_ids, expected_low_since)
    )
    if has_expectations and hasattr(db, "refresh"):
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
    container.pending_share_json = None
    db.commit()
    return result
