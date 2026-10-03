"""Persist shared, physical-GPU samples independently of idle-reclaim policy."""
import json
import logging
import math
from datetime import datetime, timedelta

from app.config import AGENT_API_TOKEN, MASTER_API_URL, NODE_ID, NODE_ROLE
from app.database_models import ComputeNodeModel, ContainerModel, GPUHistorySampleModel, UserModel
from app.node_service import verified_owners
from app.remote_agent import RemoteAgentClient, RemoteAgentError

logger = logging.getLogger(__name__)


def _number(value, maximum=None):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0
            or (maximum is not None and value > maximum)):
        return None
    return int(value)


def _owners(db, node_id, inventory):
    runtime = {row.get("container_id"): row for row in inventory.get("containers", [])
               if row.get("container_id") and row.get("status") == "running"
               and not str(row.get("name") or "").endswith("-merge-old")}
    candidates = [(owner, NODE_ID) for owner in verified_owners(db, node_id, list(runtime.values()))]
    candidates.extend((owner, node_id) for owner in inventory.get("owners", []) if isinstance(owner, dict))
    if NODE_ROLE == "worker" and node_id == NODE_ID and MASTER_API_URL and AGENT_API_TOKEN:
        try:
            feed = RemoteAgentClient(MASTER_API_URL, AGENT_API_TOKEN).owners_for_worker(NODE_ID)
            if isinstance(feed, dict) and feed.get("node_id") == NODE_ID:
                candidates.extend((owner, "master") for owner in feed.get("owners", []) if isinstance(owner, dict))
        except (RemoteAgentError, ValueError, TypeError):
            logger.warning("GPU history: master ownership feed unavailable")
    result = {}
    seen = set()
    for owner, origin in candidates:
        cid = owner.get("container_id")
        row = runtime.get(cid)
        if (cid in seen or not row or not owner.get("username")
                or owner.get("node_id", node_id) != node_id
                or row.get("name") != owner.get("container_name")
                or row.get("username") != owner.get("username")):
            continue
        try:
            indices = [int(part) for part in str(row.get("gpu_ids") or "").split(",") if part.strip()]
        except ValueError:
            continue
        seen.add(cid)
        identity = {"origin": origin, "username": owner["username"],
                    "display_name": owner.get("display_name") or owner["username"],
                    "real_name": owner.get("real_name")}
        for index in indices:
            entry = result.setdefault(index, {}).setdefault(
                (origin, owner["username"]), {**identity, "container_ids": []})
            entry["container_ids"].append(cid)
    return result


def clear_container_history(db, container=None, *, docker_id=None):
    """Remove only the exiting allocation, preserving other users on a shared card.

    Called after a successful runtime stop/removal, in the caller's transaction.
    Legacy samples without container IDs are removed when the last local
    allocation for that user/card exits.
    """
    docker_id = docker_id or (container.container_id if container else None)
    if not docker_id:
        return
    node_id = (container.node_id or NODE_ID) if container else NODE_ID
    username = None
    remaining_gpus = set()
    if container is not None:
        user = db.query(UserModel).filter(UserModel.id == container.user_id).first()
        username = user.username if user else None
        for other in db.query(ContainerModel).filter(
                ContainerModel.node_id == node_id,
                ContainerModel.user_id == container.user_id,
                ContainerModel.id != container.id,
                ContainerModel.status.in_(["running", "merging"])).all():
            remaining_gpus.update(int(part) for part in (other.gpu_ids or "").split(",") if part.strip())
    for row in db.query(GPUHistorySampleModel).filter_by(node_id=node_id).all():
        owners = json.loads(row.owners_json)
        retained = []
        for owner in owners:
            last_local_allocation = (username and owner.get("username") == username
                                     and owner.get("origin") == NODE_ID
                                     and row.gpu_index not in remaining_gpus)
            ids = owner.get("container_ids")
            if last_local_allocation:
                continue
            if ids is not None and docker_id in ids:
                owner = {**owner, "container_ids": [cid for cid in ids if cid != docker_id]}
                if not owner["container_ids"]:
                    continue
            retained.append(owner)
        if retained != owners:
            if retained:
                row.owners_json = json.dumps(retained, ensure_ascii=False)
            else:
                db.delete(row)


def resize_container_history(db, node_id, old_id, replacement_id, gpu_ids):
    """Keep history on retained cards across rebuilds, clear released cards."""
    keep = set(gpu_ids)
    for row in db.query(GPUHistorySampleModel).filter_by(node_id=node_id).all():
        owners = json.loads(row.owners_json)
        retained = []
        for owner in owners:
            ids = owner.get("container_ids", [])
            if old_id in ids:
                ids = [cid for cid in ids if cid != old_id]
                if row.gpu_index in keep:
                    ids.append(replacement_id)
                if not ids:
                    continue
                owner = {**owner, "container_ids": list(dict.fromkeys(ids))}
            retained.append(owner)
        if retained != owners:
            if retained:
                row.owners_json = json.dumps(retained, ensure_ascii=False)
            else:
                db.delete(row)


def collect_history(db, inventory_loader, now):
    """Fetch each enabled node once; return the same snapshots for reclaim checks.

    Persistence errors must not invalidate a successful live observation or disable
    the existing final, uncached safety recheck before reclaim.
    """
    inventories = {}
    bucket = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    for node in db.query(ComputeNodeModel).filter(ComputeNodeModel.enabled.is_(True)).all():
        try:
            inventories[node.id] = inventory_loader(db, node)
        except (RemoteAgentError, RuntimeError, ValueError):
            inventories[node.id] = None
            continue
        inventory = inventories[node.id]
        try:
            owners = _owners(db, node.id, inventory)
            # Reconcile externally stopped containers once runtime is observed.
            for row in db.query(GPUHistorySampleModel).filter_by(node_id=node.id).all():
                previous = json.loads(row.owners_json)
                retained = []
                active = owners.get(row.gpu_index, {})
                for owner in previous:
                    current = active.get((owner["origin"], owner["username"]))
                    # An unavailable ownership feed is not evidence of release.
                    runtime_ids = [item.get("container_id") for item in inventory.get("containers", [])
                                   if item.get("status") == "running"
                                   and item.get("username") == owner["username"]
                                   and str(row.gpu_index) in str(item.get("gpu_ids") or "").split(",")
                                   and not str(item.get("name") or "").endswith("-merge-old")]
                    active_ids = current["container_ids"] if current else runtime_ids
                    if not active_ids:
                        continue
                    if "container_ids" in owner:
                        ids = [cid for cid in owner["container_ids"] if cid in active_ids]
                        if not ids:
                            continue
                        owner = {**owner, "container_ids": ids}
                    retained.append(owner)
                if retained != previous:
                    if retained:
                        row.owners_json = json.dumps(retained, ensure_ascii=False)
                    else:
                        db.delete(row)
            db.flush()
            existing = {row.gpu_index for row in db.query(GPUHistorySampleModel).filter_by(
                node_id=node.id, sampled_at=bucket).all()}
            for gpu in inventory.get("gpus", []):
                index = int(gpu["index"])
                if index in existing:
                    continue
                existing.add(index)
                db.add(GPUHistorySampleModel(
                    node_id=node.id, node_name=node.name, gpu_index=index,
                    gpu_name=gpu.get("name") or f"GPU {index}", sampled_at=bucket,
                    utilization=_number(gpu.get("utilization"), 100),
                    memory_used_mb=_number(gpu.get("memory_used_mb")),
                    memory_total_mb=_number(gpu.get("memory_total_mb")),
                    owners_json=json.dumps(list(owners.get(index, {}).values()), ensure_ascii=False),
                ))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("GPU history persistence failed for node %s", node.id)
    db.query(GPUHistorySampleModel).filter(
        GPUHistorySampleModel.sampled_at < now - timedelta(hours=25)).delete(synchronize_session=False)
    db.commit()
    return inventories


def _iso(value):
    # Existing database timestamps are local naive datetimes; attach local offset.
    return value.astimezone().isoformat()


def history_response(db, now=None):
    now = now or datetime.now()
    since = now - timedelta(hours=24)
    rows = db.query(GPUHistorySampleModel).filter(
        GPUHistorySampleModel.sampled_at >= since,
        GPUHistorySampleModel.sampled_at <= now,
    ).order_by(GPUHistorySampleModel.sampled_at, GPUHistorySampleModel.id).all()
    series = {}
    for row in rows:
        total, used = row.memory_total_mb, row.memory_used_mb
        memory_percent = round(used / total * 100, 1) if total and used is not None and used <= total else None
        point = {"timestamp": _iso(row.sampled_at), "utilization": row.utilization,
                 "memory_percent": memory_percent, "memory_used_mb": used, "memory_total_mb": total}
        for owner in json.loads(row.owners_json):
            key = (row.node_id, row.gpu_index, owner["origin"], owner["username"])
            if key not in series:
                series[key] = {"node_id": row.node_id, "node_name": row.node_name,
                               "gpu_index": row.gpu_index, "gpu_name": row.gpu_name,
                               **owner, "points": []}
            series[key]["points"].append(point)
    return {"since": _iso(since), "until": _iso(now), "interval_minutes": 5,
            "series": [series[key] for key in sorted(series)]}
