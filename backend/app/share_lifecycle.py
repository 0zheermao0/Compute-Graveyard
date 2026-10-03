"""Invalidate unfinished approvals when an original GPU occupancy exits.

Call under worker_gpu_lock, after successful runtime removal/stop, before the
caller commits or clears the exiting container's Docker ID. No API dependency.
"""
import json
from datetime import datetime
from functools import wraps

from app.config import NODE_ID
from app.database_models import ContainerModel, ShareRequestModel
from app.worker_share import worker_gpu_lock


def synchronized_occupancy_exit(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with worker_gpu_lock:
            return function(*args, **kwargs)
    return wrapped


def _object(value, default):
    try:
        result = json.loads(value or "null")
        return result if isinstance(result, type(default)) else default
    except (ValueError, TypeError):
        return default


def _gpus(value):
    return {part.strip() for part in str(value or "").split(",") if part.strip()}


def _records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def reject_shares_for_exit(db, container=None, *, docker_id=None, now=None):
    now = now or datetime.now()
    docker_id = docker_id or (container.container_id if container else None)
    from app.gpu_history import clear_container_history
    clear_container_history(db, container, docker_id=docker_id)
    if container is not None:
        node_id = container.node_id or NODE_ID
        for pending in db.query(ContainerModel).filter(
                ContainerModel.status == "pending_share_approval",
                ContainerModel.node_id == node_id).all():
            payload = _object(pending.pending_share_json, {})
            # Remote approval is authoritative on the Worker.
            if payload.get("request_id"):
                continue
            target_match = pending.target_container_id == container.id
            snapshot = payload.get("occupancy")
            if isinstance(snapshot, list):
                owner_match = any(item.get("id") == container.id for item in _records(snapshot))
            else:
                owner_match = any(str(a.get("user_id")) == str(container.user_id)
                                  for a in _records(payload.get("approvers")))
            if not target_match and not (owner_match and _gpus(pending.gpu_ids) & _gpus(container.gpu_ids)):
                continue
            suffix = f"-rej-{pending.id}-{int(now.timestamp())}"
            pending.name = pending.name[:max(1, 128 - len(suffix))] + suffix
            pending.status = "share_rejected"
            pending.pending_share_json = None
            pending.stopped_at = now
    if not docker_id or (container is not None and (container.node_id or NODE_ID) != NODE_ID):
        return
    for request in db.query(ShareRequestModel).filter(
            ShareRequestModel.state.in_(["pending", "approved"]),
            ShareRequestModel.provision_result.is_(None)).all():
        payload = _object(request.payload, {})
        approvers = _object(request.approvers, [])
        if (any(item.get("container_id") == docker_id for item in _records(payload.get("occupancy"))) or
                any(isinstance(a.get("container_ids"), list) and docker_id in a["container_ids"]
                    for a in _records(approvers))):
            request.state = "rejected"
