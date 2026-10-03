"""资源看板 API"""
from collections import defaultdict
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends
from sqlalchemy import func

from app.auth import get_current_user
from app.database import get_db
from app.database_models import ContainerModel, ShareRequestModel, UserModel, UserNotificationModel
from app.api.containers import _parse_share_payload, _remote_share_status, lease_reminder_active, pending_share_approver_ids, remote_share_approver_ids
from app.docker_service import get_gpu_info, get_system_load
from app.database import get_setting
from app.models import (
    DashboardResponse,
    DashboardNode,
    GPUInfo,
    GPUSharingStatus,
    SystemLoad,
    ContainerOccupancy,
    RunningContainerContact,
    UsageRankItem,
    DiskRankItem,
    GPUUtilRankItem,
    ReminderRankItem,
)
from app.config import AGENT_API_TOKEN, DEFAULT_MAX_GPU_SHARING_USERS, MASTER_API_URL, NODE_ID, NODE_NAME, NODE_PUBLIC_HOST, NODE_ROLE
from app.node_service import _inventory_occupied_gpu_ids, aggregate_inventories, local_inventory, verified_owners, worker_share_snapshot
from app.remote_agent import RemoteAgentClient, RemoteAgentError
from datetime import datetime, timedelta

router = APIRouter()
_remote_reminder_lock = Lock()
_remote_reminder_cache = {}


def _remote_pending_actionable(db, container, payload):
    key = (container.id, container.node_id, payload["request_id"], repr(payload["occupancy"]))
    with _remote_reminder_lock:
        now = monotonic()
        for cached_key, (expires, _) in list(_remote_reminder_cache.items()):
            if expires <= now:
                del _remote_reminder_cache[cached_key]
        cached = _remote_reminder_cache.get(key)
        if cached:
            return cached[1]
        try:
            _, _, status = _remote_share_status(db, container, payload)
            actionable = status.get("state") in ("pending", "approved")
        except (RemoteAgentError, ValueError, TypeError, KeyError):
            actionable = False
        _remote_reminder_cache[key] = (monotonic() + 30, actionable)
        return actionable


def _container_duration_hours(c: ContainerModel, now: datetime) -> float:
    """计算容器已使用时长（小时）"""
    start = c.created_at
    end = now if c.status in {"running", "merging"} else (c.stopped_at or now)
    if not start or not end:
        return 0.0
    delta = end - start
    return max(0, delta.total_seconds() / 3600)


def _distinct_users_per_gpu(db, node_id: str = NODE_ID) -> dict:
    """gpu_index -> set of user_id（仅统计运行中且占用该卡的容器）"""
    from collections import defaultdict

    m = defaultdict(set)
    for c in db.query(ContainerModel).filter(ContainerModel.status.in_(["running", "merging"]), ContainerModel.node_id == node_id).all():
        if not c.gpu_ids:
            continue
        for gid in map(int, c.gpu_ids.split(",")):
            m[gid].add(c.user_id)
    return m


def _node_gpu_sharing(inventory: dict, owners: list[dict], node_id: str, max_share: int, local_owned_ids: set[str] | None = None) -> list[GPUSharingStatus]:
    runtime = {row.get("container_id"): row for row in inventory.get("containers", []) if row.get("container_id") and row.get("status") == "running" and not str(row.get("name") or "").endswith("-merge-old")}
    known_ids = {owner["container_id"] for owner in owners}
    if local_owned_ids is None:
        local_owned_ids = set()
    users_per_gpu = defaultdict(set)
    for owner in owners:
        for value in str(runtime[owner["container_id"]].get("gpu_ids") or "").split(","):
            if value.strip():
                users_per_gpu[int(value)].add((owner.get("_origin", node_id), owner["username"]))

    occupied_ids = _inventory_occupied_gpu_ids(inventory)
    external_per_gpu = defaultdict(set)
    unknown_per_gpu = defaultdict(set)
    for index, row in enumerate(inventory.get("containers", [])):
        if row.get("status") not in {"running", "merging"} and not str(row.get("name") or "").endswith("-merge-old"):
            continue
        cid = row.get("container_id")
        if cid in local_owned_ids and row.get("status") == "running" and not str(row.get("name") or "").endswith("-merge-old"):
            continue
        for value in str(row.get("gpu_ids") or "").split(","):
            if value.strip() and int(value) in occupied_ids:
                gpu_id = int(value)
                external_per_gpu[gpu_id].add(cid or index)
                if cid not in known_ids:
                    unknown_per_gpu[gpu_id].add(cid or index)

    result = []
    for row in inventory.get("gpus", []):
        gpu_id = int(row["index"])
        external_count = len(external_per_gpu[gpu_id])
        occupant_count = len(users_per_gpu[gpu_id]) + len(unknown_per_gpu[gpu_id])
        result.append(GPUSharingStatus(
            gpu_index=gpu_id,
            occupant_count=occupant_count,
            max_sharing=max_share,
            selectable=external_count == 0 and occupant_count < max_share,
            external_occupied=external_count > 0,
            unknown_occupant_count=len(unknown_per_gpu[gpu_id]),
        ))
    return result


def _compute_ranking(db, since: datetime) -> list:
    """计算自 since 以来各用户累计使用时长排行"""
    containers = db.query(ContainerModel).filter(ContainerModel.created_at >= since).all()
    now = datetime.now()
    user_hours = defaultdict(float)
    user_info = {}
    for c in containers:
        owner = db.query(UserModel).filter(UserModel.id == c.user_id).first()
        if not owner:
            continue
        hours = _container_duration_hours(c, now)
        user_hours[owner.username] += hours
        user_info[owner.username] = getattr(owner, "real_name", None) or owner.display_name or owner.username
    sorted_users = sorted(user_hours.items(), key=lambda x: -x[1])
    return [
        UsageRankItem(rank=i + 1, username=u, real_name=user_info.get(u), total_hours=round(h, 1))
        for i, (u, h) in enumerate(sorted_users[:20])
    ]


def _reminder_ranking(db, now: datetime, local_only: bool = False) -> list[ReminderRankItem]:
    users = db.query(UserModel).all()
    counts = {user.id: 0 for user in users}
    # 所有类型的未读站内事件均计入，已读历史通知不再提醒。
    unread_events = (
        db.query(UserNotificationModel.user_id, func.count(UserNotificationModel.id))
        .filter(UserNotificationModel.read_at.is_(None))
        .group_by(UserNotificationModel.user_id)
        .all()
    )
    for user_id, unread_count in unread_events:
        if user_id in counts:
            counts[user_id] += unread_count
    for container in db.query(ContainerModel).filter(ContainerModel.status == "pending_share_approval").all():
        payload = _parse_share_payload(container.pending_share_json)
        if not payload:
            continue
        if NODE_ROLE == "master" and payload.get("request_id") and (local_only or not _remote_pending_actionable(db, container, payload)):
            continue
        if container.user_id in counts:
            counts[container.user_id] += 1
        try:
            approver_ids = pending_share_approver_ids(payload)
        except (ValueError, TypeError, KeyError):
            approver_ids = set()
        for user_id in approver_ids:
            if user_id in counts:
                counts[user_id] += 1
    for container in db.query(ContainerModel).filter(ContainerModel.status == "running").all():
        if container.user_id in counts and lease_reminder_active(container, now):
            counts[container.user_id] += 1
    if NODE_ROLE == "worker":
        for row in db.query(ShareRequestModel).filter(ShareRequestModel.state == "pending").all():
            for user_id in remote_share_approver_ids(db, row):
                if user_id in counts:
                    counts[user_id] += 1
    ranked = sorted((user for user in users if counts[user.id]), key=lambda user: (-counts[user.id], user.username, user.id))[:20]
    return [ReminderRankItem(username=user.username, real_name=user.real_name or user.display_name or user.username, unread_count=counts[user.id]) for user in ranked]


def _disk_ranking(db) -> list[DiskRankItem]:
    users = db.query(UserModel).filter(UserModel.disk_usage_scan_complete.is_(True)).all()
    users.sort(key=lambda user: (-user.disk_usage_bytes, user.username))
    return [
        DiskRankItem(rank=index, username=user.username, real_name=user.real_name or user.display_name or user.username, usage_bytes=user.disk_usage_bytes)
        for index, user in enumerate(users[:20], 1)
    ]


def _estimated_gpu_ranking(nodes: list[DashboardNode], now: datetime, days: int) -> list[GPUUtilRankItem]:
    since = now - timedelta(days=days)
    weighted = defaultdict(float)
    hours_by_user = defaultdict(float)
    names = {}
    for node in nodes:
        if not node.online:
            continue
        gpus = {gpu.index: gpu for gpu in node.gpus}
        occupants = defaultdict(dict)
        for occupancy in node.occupancies:
            if not occupancy.created_at or occupancy.created_at >= now:
                continue
            starts = occupants[occupancy.gpu_index]
            user = (occupancy.origin or node.node_id, occupancy.username)
            starts[user] = min(starts.get(user, now), occupancy.created_at)
            names[user] = occupancy.real_name or occupancy.display_name or occupancy.username
        for gpu_index, starts in occupants.items():
            gpu = gpus.get(gpu_index)
            if not gpu or gpu.utilization is None or not starts:
                continue
            utilization = max(0, min(100, gpu.utilization))
            memory_ratio = (max(0, min(1, gpu.memory_used_mb / gpu.memory_total_mb))
                            if gpu.memory_used_mb is not None and gpu.memory_total_mb and gpu.memory_total_mb > 0 else 0)
            estimate = utilization * (0.7 + 0.3 * memory_ratio) / len(starts)
            for user, start in starts.items():
                hours = max(0, (now - max(start, since)).total_seconds() / 3600)
                if hours:
                    weighted[user] += estimate * hours
                    hours_by_user[user] += hours
    ranked = sorted(hours_by_user, key=lambda user: (-weighted[user] / hours_by_user[user], user))[:20]
    duplicate_names = {user for user in ranked if sum(names[other] == names[user] or other[1] == user[1] for other in ranked) > 1}
    return [
        GPUUtilRankItem(rank=index, username=user[1], real_name=f"{names[user]} ({user[0]}: {user[1]})" if user in duplicate_names else names[user], estimated_percent=round(weighted[user] / hours_by_user[user], 1))
        for index, user in enumerate(ranked, 1)
    ]


@router.get("/gpu-history")
def get_gpu_history(db=Depends(get_db), _=Depends(get_current_user)):
    from app.gpu_history import history_response
    return history_response(db)


@router.get("", response_model=DashboardResponse)
def get_dashboard(db=Depends(get_db), _=Depends(get_current_user), local_only: bool = False):
    gpu_rows = get_gpu_info()
    gpus = [GPUInfo(**g) for g in gpu_rows] if gpu_rows else []
    load = get_system_load()
    system_load = SystemLoad(**load)
    now = datetime.now()

    occupancies = []
    occupancies_by_node = defaultdict(list)
    all_containers = []
    accepted_owners = defaultdict(list)
    local_owned_ids = defaultdict(set)
    if NODE_ROLE == "master":
        inventory_items = aggregate_inventories(db, local_only=True) if local_only else aggregate_inventories(db)
    else:
        try:
            inventory_items = [{"node": {"id": NODE_ID, "name": NODE_NAME, "public_host": NODE_PUBLIC_HOST, "schedulable": True, "is_local": True}, "online": True, "inventory": local_inventory(db)}]
        except RuntimeError:
            inventory_items = [{"node": {"id": NODE_ID, "name": NODE_NAME, "public_host": NODE_PUBLIC_HOST, "schedulable": True, "is_local": True}, "online": False, "inventory": None}]
    for item in inventory_items:
        inventory = item.get("inventory") or {}
        node_id = item["node"]["id"]
        runtime = {row.get("container_id"): row for row in inventory.get("containers", []) if row.get("container_id") and row.get("status") == "running" and not str(row.get("name") or "").endswith("-merge-old")}
        inventory_owners = inventory.get("owners", []) if isinstance(inventory.get("owners", []), list) else []
        owners = [(owner, NODE_ID, True) for owner in verified_owners(db, node_id, inventory.get("containers", []))]
        owners.extend((owner, node_id, False) for owner in inventory_owners)
        if NODE_ROLE == "worker" and not local_only and item["online"] and MASTER_API_URL and AGENT_API_TOKEN:
            try:
                feed = RemoteAgentClient(MASTER_API_URL, AGENT_API_TOKEN).owners_for_worker(NODE_ID)
                if isinstance(feed, dict) and feed.get("node_id") == NODE_ID and isinstance(feed.get("owners"), list):
                    owners.extend((owner, "master", False) for owner in feed["owners"])
            except (RemoteAgentError, ValueError):
                pass
        db_identities = {
            container.container_id: (container.name, user.username, container.status)
            for container, user in db.query(ContainerModel, UserModel).join(UserModel, ContainerModel.user_id == UserModel.id).filter(
                ContainerModel.node_id == node_id, ContainerModel.container_id.in_(runtime)
            ).all()
        }
        seen = set()
        for owner, origin, local_owned in owners:
            if not isinstance(owner, dict):
                continue
            cid = owner.get("container_id")
            row = runtime.get(cid)
            if (not cid or cid in seen or not row or owner.get("node_id", node_id) != node_id
                    or not owner.get("username") or row.get("name") != owner.get("container_name")
                    or row.get("username") != owner.get("username")):
                continue
            identity = db_identities.get(cid)
            if identity and identity != (owner["container_name"], owner["username"], "running"):
                continue
            try:
                created_at = datetime.fromisoformat(owner["created_at"]) if owner.get("created_at") else None
                expires_at = datetime.fromisoformat(owner["expires_at"])
                gpu_ids = str(row.get("gpu_ids") or "")
                gpu_indices = [int(value) for value in gpu_ids.split(",") if value.strip()]
                duration = round(max(0, (now - created_at).total_seconds() / 3600), 1) if created_at else None
                details = {key: owner.get(key) for key in ("username", "display_name", "real_name", "contact_type", "contact_value")}
                details["display_name"] = details["display_name"] or details["username"]
                contact = RunningContainerContact(container_name=owner["container_name"], gpu_ids=gpu_ids or "CPU", created_at=created_at, duration_hours=duration, expires_at=expires_at, ssh_port=owner.get("ssh_port"), **details)
                node_occupancies = [ContainerOccupancy(gpu_index=gid, container_name=owner["container_name"], origin=origin, created_at=created_at, duration_hours=duration, expires_at=expires_at, ssh_port=owner.get("ssh_port"), **details) for gid in gpu_indices]
            except (ValueError, TypeError, KeyError):
                continue
            seen.add(cid)
            accepted_owners[node_id].append({**owner, "_origin": origin})
            if local_owned:
                local_owned_ids[node_id].add(cid)
            all_containers.append(contact)
            occupancies.extend(node_occupancies)
            occupancies_by_node[node_id].extend(node_occupancies)

    weekly = _compute_ranking(db, now - timedelta(days=7))
    monthly = _compute_ranking(db, now - timedelta(days=30))

    max_share = int(get_setting("max_gpu_sharing_users", str(DEFAULT_MAX_GPU_SHARING_USERS)))
    if max_share < 1:
        max_share = DEFAULT_MAX_GPU_SHARING_USERS

    users_per_gpu = _distinct_users_per_gpu(db)
    gpu_sharing_list: list[GPUSharingStatus] = []
    for g in gpu_rows or []:
        idx = int(g["index"])
        occ = len(users_per_gpu.get(idx, set()))
        gpu_sharing_list.append(
            GPUSharingStatus(
                gpu_index=idx,
                occupant_count=occ,
                max_sharing=max_share,
                selectable=occ < max_share,
            )
        )

    if NODE_ROLE == "master":
        node_rows = []
        for item in inventory_items:
            inventory = item.get("inventory") or {}
            node = item["node"]
            node_gpu_sharing = _node_gpu_sharing(inventory, accepted_owners[node["id"]], node["id"], max_share, local_owned_ids[node["id"]])
            if not node["is_local"] and item["online"] and node["schedulable"]:
                for sharing in node_gpu_sharing:
                    if not sharing.external_occupied or sharing.unknown_occupant_count or sharing.occupant_count >= max_share:
                        continue
                    try:
                        snapshot = worker_share_snapshot(db, node["id"], inventory, [sharing.gpu_index])
                        sharing.worker_shareable = bool(snapshot) and len({row["username"] for row in snapshot}) < max_share
                    except (ValueError, TypeError, KeyError, AttributeError):
                        pass
            if node["is_local"] and item["online"]:
                gpu_sharing_list = node_gpu_sharing
            node_rows.append(
                DashboardNode(
                    node_id=node["id"],
                    node_name=node["name"],
                    online=item["online"],
                    schedulable=node["schedulable"],
                    public_host=node["public_host"] or None,
                    is_local=node["is_local"],
                    gpus=[GPUInfo(**row) for row in inventory.get("gpus", [])],
                    system_load=SystemLoad(**inventory["system_load"]) if inventory.get("system_load") else None,
                    container_count=len(inventory.get("containers", [])),
                    occupancies=occupancies_by_node[node["id"]],
                    gpu_sharing=node_gpu_sharing,
                    error=item.get("error"),
                )
            )
    else:
        local_item = inventory_items[0]
        local_inv = local_item.get("inventory") or {}
        gpu_sharing_list = _node_gpu_sharing(local_inv, accepted_owners[NODE_ID], NODE_ID, max_share, local_owned_ids[NODE_ID]) if local_item["online"] else []
        node_rows = [
            DashboardNode(
                node_id=NODE_ID,
                node_name=NODE_NAME,
                online=local_item["online"],
                schedulable=True,
                public_host=NODE_PUBLIC_HOST,
                is_local=True,
                gpus=gpus,
                system_load=system_load,
                container_count=len(local_inv.get("containers", [])),
                occupancies=occupancies_by_node[NODE_ID],
                gpu_sharing=gpu_sharing_list,
            )
        ]

    return DashboardResponse(
        gpus=gpus,
        system_load=system_load,
        occupancies=occupancies,
        all_containers=all_containers,
        weekly_ranking=weekly,
        monthly_ranking=monthly,
        disk_ranking=_disk_ranking(db),
        weekly_gpu_ranking=_estimated_gpu_ranking(node_rows, now, 7),
        monthly_gpu_ranking=_estimated_gpu_ranking(node_rows, now, 30),
        reminder_ranking=_reminder_ranking(db, now, local_only=local_only),
        gpu_sharing=gpu_sharing_list,
        max_gpu_sharing_users=max_share,
        nodes=node_rows,
    )
