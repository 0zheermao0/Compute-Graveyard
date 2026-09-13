"""资源看板 API"""
from collections import defaultdict

from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.database import get_db
from app.database_models import ContainerModel, UserModel
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
)
from app.config import DEFAULT_MAX_GPU_SHARING_USERS, NODE_ID, NODE_NAME, NODE_PUBLIC_HOST, NODE_ROLE
from app.node_service import aggregate_inventories
from datetime import datetime, timedelta

router = APIRouter()


def _container_duration_hours(c: ContainerModel, now: datetime) -> float:
    """计算容器已使用时长（小时）"""
    start = c.created_at
    end = now if c.status == "running" else (c.stopped_at or now)
    if not start or not end:
        return 0.0
    delta = end - start
    return max(0, delta.total_seconds() / 3600)


def _distinct_users_per_gpu(db, node_id: str = NODE_ID) -> dict:
    """gpu_index -> set of user_id（仅统计运行中且占用该卡的容器）"""
    from collections import defaultdict

    m = defaultdict(set)
    for c in db.query(ContainerModel).filter(ContainerModel.status == "running", ContainerModel.node_id == node_id).all():
        if not c.gpu_ids:
            continue
        for gid in map(int, c.gpu_ids.split(",")):
            m[gid].add(c.user_id)
    return m


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


@router.get("", response_model=DashboardResponse)
def get_dashboard(db=Depends(get_db), _=Depends(get_current_user)):
    gpu_rows = get_gpu_info()
    gpus = [GPUInfo(**g) for g in gpu_rows] if gpu_rows else []
    load = get_system_load()
    system_load = SystemLoad(**load)
    now = datetime.now()

    occupancies = []
    occupancies_by_node = defaultdict(list)
    all_containers = []
    containers = db.query(ContainerModel).filter(ContainerModel.status == "running").all()
    for c in containers:
        owner = db.query(UserModel).filter(UserModel.id == c.user_id).first()
        if not owner:
            continue
        uname = owner.username
        dname = owner.display_name or uname
        rname = getattr(owner, "real_name", None) or ""
        ctype = getattr(owner, "contact_type", None) or ""
        cval = getattr(owner, "contact_value", None) or ""
        dur = _container_duration_hours(c, now)
        if c.gpu_ids:
            for gid in map(int, c.gpu_ids.split(",")):
                occupancy = ContainerOccupancy(
                    gpu_index=gid,
                    container_name=c.name,
                    username=uname,
                    display_name=dname,
                    real_name=rname or None,
                    contact_type=ctype or None,
                    contact_value=cval or None,
                    created_at=c.created_at,
                    duration_hours=round(dur, 1),
                    expires_at=c.expires_at,
                    ssh_port=c.ssh_port,
                )
                occupancies.append(occupancy)
                occupancies_by_node[c.node_id or NODE_ID].append(occupancy)
        all_containers.append(
            RunningContainerContact(
                container_name=c.name,
                username=uname,
                display_name=dname,
                real_name=rname or None,
                contact_type=ctype or None,
                contact_value=cval or None,
                gpu_ids=c.gpu_ids or "CPU",
                created_at=c.created_at,
                duration_hours=round(dur, 1),
                expires_at=c.expires_at,
                ssh_port=c.ssh_port,
            )
        )

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
        for item in aggregate_inventories(db):
            inventory = item.get("inventory") or {}
            node = item["node"]
            node_users_per_gpu = _distinct_users_per_gpu(db, node["id"])
            node_gpu_sharing = [
                GPUSharingStatus(
                    gpu_index=int(row["index"]),
                    occupant_count=len(node_users_per_gpu.get(int(row["index"]), set())),
                    max_sharing=max_share,
                    selectable=len(node_users_per_gpu.get(int(row["index"]), set())) < max_share,
                )
                for row in inventory.get("gpus", [])
            ]
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
        node_rows = [
            DashboardNode(
                node_id=NODE_ID,
                node_name=NODE_NAME,
                online=True,
                schedulable=True,
                public_host=NODE_PUBLIC_HOST,
                is_local=True,
                gpus=gpus,
                system_load=system_load,
                container_count=len([container for container in containers if (container.node_id or NODE_ID) == NODE_ID]),
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
        gpu_sharing=gpu_sharing_list,
        max_gpu_sharing_users=max_share,
        nodes=node_rows,
    )
