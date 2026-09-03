"""容器申请 API"""
import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.database import get_db
from app.database_models import ContainerModel, UserModel
from app.docker_service import (
    create_container,
    allocate_ssh_port,
)
from app.models import (
    ContainerApplyRequest,
    ContainerApplyResult,
    ContainerResponse,
    NotificationItem,
    NotificationListResponse,
    ShareApproverInfo,
)
from app.config import (
    DEFAULT_LEASE_DAYS,
    MAX_LEASE_DAYS,
    MAX_GPUS_PER_USER,
    MAX_CONTAINERS_PER_USER,
    DEFAULT_CPU_MEM_GB,
    DEFAULT_GPU_MEM_GB_PER_GPU,
    DEFAULT_MAX_GPU_SHARING_USERS,
)
from app.database import get_setting

router = APIRouter()


def _max_gpu_sharing(db) -> int:
    v = int(get_setting("max_gpu_sharing_users", str(DEFAULT_MAX_GPU_SHARING_USERS)))
    return max(1, v)


def _distinct_users_per_gpu_map(db) -> dict[int, set[int]]:
    from collections import defaultdict

    m = defaultdict(set)
    for c in db.query(ContainerModel).filter(ContainerModel.status == "running").all():
        if not c.gpu_ids:
            continue
        for gid in map(int, c.gpu_ids.split(",")):
            m[gid].add(c.user_id)
    return dict(m)


def _capacity_ok(gpu_ids: list[int], applicant_id: int, max_share: int, users_per_gpu: dict[int, set[int]]) -> bool:
    """同一 GPU 上不同用户数不超过上限；申请人已在该卡上则不要求新增名额。"""
    for gid in gpu_ids:
        users = users_per_gpu.get(gid, set())
        if applicant_id in users:
            if len(users) > max_share:
                return False
        else:
            if len(users) >= max_share:
                return False
    return True


def _occupiers_for_gpus(db, gpu_ids: list[int], applicant_id: int) -> list[tuple[int, str]]:
    """在所选 GPU 上有运行中容器的其他用户（需征求同意），按 user_id 排序。"""
    seen: dict[int, str] = {}
    want = set(gpu_ids)
    for c in db.query(ContainerModel).filter(ContainerModel.status == "running").all():
        if c.user_id == applicant_id or not c.gpu_ids:
            continue
        cg = set(int(x) for x in c.gpu_ids.split(","))
        if want & cg:
            if c.user_id not in seen:
                owner = db.query(UserModel).filter(UserModel.id == c.user_id).first()
                seen[c.user_id] = owner.username if owner else str(c.user_id)
    return sorted(seen.items(), key=lambda x: x[0])


def _parse_share_payload(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _response_from_container(db, c: ContainerModel, owner_username: str | None = None) -> ContainerResponse:
    ep = json.loads(c.extra_ports) if c.extra_ports else {}
    ep_int = {int(k): v for k, v in ep.items()} if ep else None
    share_list: list[ShareApproverInfo] | None = None
    pending_days: int | None = None
    if c.status == "pending_share_approval" and c.pending_share_json:
        payload = _parse_share_payload(c.pending_share_json)
        if payload:
            pending_days = int(payload.get("lease_days", DEFAULT_LEASE_DAYS))
            share_list = []
            for a in payload.get("approvers") or []:
                at = a.get("approved_at")
                share_list.append(
                    ShareApproverInfo(
                        user_id=int(a["user_id"]),
                        username=str(a.get("username", "")),
                        approved=bool(a.get("approved")),
                        approved_at=datetime.fromisoformat(at) if isinstance(at, str) and at else None,
                    )
                )
    uname = owner_username
    if uname is None:
        u = db.query(UserModel).filter(UserModel.id == c.user_id).first()
        uname = u.username if u else ""

    return ContainerResponse(
        id=c.id,
        name=c.name,
        container_id=c.container_id,
        gpu_ids=c.gpu_ids or "",
        ssh_port=c.ssh_port or 0,
        ssh_password=c.ssh_password,
        extra_ports=ep_int,
        status=c.status,
        expires_at=c.expires_at,
        owner_username=uname or "",
        created_at=c.created_at,
        share_approvers=share_list,
        pending_lease_days=pending_days,
    )


def _provision_running_container(db, c: ContainerModel, lease_days: int) -> None:
    """由待审批记录实际创建 Docker 容器并改为 running。"""
    user = db.query(UserModel).filter(UserModel.id == c.user_id).first()
    if not user:
        raise HTTPException(status_code=500, detail="用户不存在")

    gpu_ids = [int(x) for x in c.gpu_ids.split(",")] if c.gpu_ids else []

    users_per_gpu = _distinct_users_per_gpu_map(db)
    mx = _max_gpu_sharing(db)
    if not _capacity_ok(gpu_ids, c.user_id, mx, users_per_gpu):
        raise HTTPException(status_code=400, detail="审批完成时 GPU 已无可用共用名额，请申请人重新申请")

    ssh_port = allocate_ssh_port()
    if not ssh_port:
        raise HTTPException(status_code=500, detail="暂无可用 SSH 端口")

    if gpu_ids:
        gpu_mem_gb_per_gpu = int(get_setting("gpu_mem_gb_per_gpu", str(DEFAULT_GPU_MEM_GB_PER_GPU)))
        mem_limit_gb = gpu_mem_gb_per_gpu * len(gpu_ids)
    else:
        mem_limit_gb = int(get_setting("cpu_mem_gb", str(DEFAULT_CPU_MEM_GB)))

    prefix = "labcpu" if not gpu_ids else "labgpu"
    safe_name = f"{prefix}-{user.username}-{datetime.now().strftime('%Y%m%d%H%M')}"

    try:
        container_id, ssh_password, extra_ports = create_container(
            name=safe_name,
            username=user.username,
            gpu_ids=gpu_ids,
            ssh_port=ssh_port,
            mem_limit_gb=mem_limit_gb,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    extra_ports_json = json.dumps({str(k): v for k, v in extra_ports.items()}) if extra_ports else None
    expires_at = datetime.now() + timedelta(days=lease_days)

    c.name = safe_name
    c.container_id = container_id
    c.ssh_port = ssh_port
    c.ssh_password = ssh_password
    c.extra_ports = extra_ports_json
    c.status = "running"
    c.pending_share_json = None
    c.expires_at = expires_at
    db.commit()


@router.post("/apply", response_model=ContainerApplyResult)
def apply_container(req: ContainerApplyRequest, user=Depends(get_current_user), db=Depends(get_db)):
    if user.role not in ("user", "admin"):
        raise HTTPException(status_code=403, detail="无权限申请")

    if req.lease_days < 1 or req.lease_days > MAX_LEASE_DAYS:
        raise HTTPException(status_code=400, detail=f"租期须在 1~{MAX_LEASE_DAYS} 天之间")

    my_count = db.query(ContainerModel).filter(
        ContainerModel.user_id == user.id,
        ContainerModel.status.in_(["running", "pending_share_approval"]),
    ).count()
    if my_count >= MAX_CONTAINERS_PER_USER:
        raise HTTPException(status_code=400, detail=f"每人最多同时有 {MAX_CONTAINERS_PER_USER} 个运行中或待审批的容器")

    gpu_ids = [] if req.cpu_only else (req.gpu_ids or [])
    if not req.cpu_only and not gpu_ids:
        raise HTTPException(status_code=400, detail="请选择 GPU 或勾选纯 CPU 容器")

    if not req.cpu_only and gpu_ids:
        my_running = db.query(ContainerModel).filter(
            ContainerModel.user_id == user.id,
            ContainerModel.status == "running",
        ).all()
        total_gpus = sum(len(c.gpu_ids.split(",")) for c in my_running if c.gpu_ids)
        if total_gpus + len(gpu_ids) > MAX_GPUS_PER_USER:
            raise HTTPException(status_code=400, detail=f"每人最多使用 {MAX_GPUS_PER_USER} 块 GPU")

    mx = _max_gpu_sharing(db)
    users_per_gpu = _distinct_users_per_gpu_map(db)
    if not _capacity_ok(gpu_ids, user.id, mx, users_per_gpu):
        raise HTTPException(status_code=400, detail="所选 GPU 已达到共用人数上限，请稍后再试或选择其他卡")

    occupiers = _occupiers_for_gpus(db, gpu_ids, user.id)

    if occupiers:
        approvers = [{"user_id": uid, "username": uname, "approved": False, "approved_at": None} for uid, uname in occupiers]
        payload = {"lease_days": req.lease_days, "approvers": approvers}
        pend_name = f"labgpu-{user.username}-pend-{datetime.now().strftime('%Y%m%d%H%M')}"
        pending_cid = f"pending-{uuid.uuid4().hex}"
        far_expires = datetime.now() + timedelta(days=3650)
        c = ContainerModel(
            container_id=pending_cid,
            name=pend_name,
            user_id=user.id,
            gpu_ids=",".join(map(str, sorted(gpu_ids))),
            ssh_port=0,
            ssh_password=None,
            extra_ports=None,
            status="pending_share_approval",
            expires_at=far_expires,
            pending_share_json=json.dumps(payload, ensure_ascii=False),
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        return ContainerApplyResult(
            container=_response_from_container(db, c, user.username),
            pending_share_approval=True,
            message="所选 GPU 上有其他同学的容器，已向对方发起共用申请，全部同意后自动创建",
        )

    ssh_port = allocate_ssh_port()
    if not ssh_port:
        raise HTTPException(status_code=500, detail="暂无可用 SSH 端口")

    expires_at = datetime.now() + timedelta(days=req.lease_days)
    prefix = "labcpu" if req.cpu_only else "labgpu"
    container_name = f"{prefix}-{user.username}-{datetime.now().strftime('%Y%m%d%H%M')}"

    if gpu_ids:
        gpu_mem_gb_per_gpu = int(get_setting("gpu_mem_gb_per_gpu", str(DEFAULT_GPU_MEM_GB_PER_GPU)))
        mem_limit_gb = gpu_mem_gb_per_gpu * len(gpu_ids)
    else:
        mem_limit_gb = int(get_setting("cpu_mem_gb", str(DEFAULT_CPU_MEM_GB)))

    try:
        container_id, ssh_password, extra_ports = create_container(
            name=container_name,
            username=user.username,
            gpu_ids=gpu_ids,
            ssh_port=ssh_port,
            mem_limit_gb=mem_limit_gb,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    extra_ports_json = json.dumps({str(k): v for k, v in extra_ports.items()}) if extra_ports else None
    c = ContainerModel(
        container_id=container_id,
        name=container_name,
        user_id=user.id,
        gpu_ids=",".join(map(str, sorted(gpu_ids))) if gpu_ids else "",
        ssh_port=ssh_port,
        ssh_password=ssh_password,
        extra_ports=extra_ports_json,
        status="running",
        expires_at=expires_at,
        pending_share_json=None,
    )
    db.add(c)
    db.commit()
    db.refresh(c)

    return ContainerApplyResult(
        container=_response_from_container(db, c, user.username),
        pending_share_approval=False,
        message=None,
    )


@router.get("/share-awaiting-my-action", response_model=list[ContainerResponse])
def list_share_requests_for_me(user=Depends(get_current_user), db=Depends(get_db)):
    """当前用户作为占用者时，待其同意的共用申请。"""
    pending = (
        db.query(ContainerModel)
        .filter(ContainerModel.status == "pending_share_approval")
        .order_by(ContainerModel.created_at.desc())
        .all()
    )
    result: list[ContainerResponse] = []
    for c in pending:
        payload = _parse_share_payload(c.pending_share_json)
        if not payload:
            continue
        ids = {int(a["user_id"]) for a in payload.get("approvers") or []}
        if user.id not in ids:
            continue
        mine = next((a for a in payload["approvers"] if int(a["user_id"]) == user.id), None)
        if mine and mine.get("approved"):
            continue
        owner = db.query(UserModel).filter(UserModel.id == c.user_id).first()
        result.append(_response_from_container(db, c, owner.username if owner else ""))
    return result


@router.get("/notifications", response_model=NotificationListResponse)
def list_notifications(user=Depends(get_current_user), db=Depends(get_db)):
    """聚合用户通知（实时计算，不落库）。"""
    items: list[NotificationItem] = []
    now = datetime.now()

    # 1) 待我同意的 GPU 共用申请
    pending = (
        db.query(ContainerModel)
        .filter(ContainerModel.status == "pending_share_approval")
        .order_by(ContainerModel.created_at.desc())
        .all()
    )
    for c in pending:
        payload = _parse_share_payload(c.pending_share_json)
        if not payload:
            continue
        approvers = payload.get("approvers") or []
        mine = next((a for a in approvers if int(a["user_id"]) == user.id), None)
        if mine and not mine.get("approved"):
            owner = db.query(UserModel).filter(UserModel.id == c.user_id).first()
            owner_name = owner.username if owner else "未知用户"
            items.append(
                NotificationItem(
                    id=f"share-approval-{c.id}",
                    type="share_approval_request",
                    title="GPU 共用申请待处理",
                    message=f"{owner_name} 申请共用 GPU {c.gpu_ids}，请同意或拒绝。",
                    created_at=c.created_at or now,
                    container_id=c.id,
                    container_name=c.name,
                    gpu_ids=c.gpu_ids or "",
                )
            )

    # 2) 我自己的容器到期前 24 小时提醒续租
    my_running = (
        db.query(ContainerModel)
        .filter(
            ContainerModel.user_id == user.id,
            ContainerModel.status == "running",
        )
        .order_by(ContainerModel.expires_at.asc())
        .all()
    )
    for c in my_running:
        if not c.expires_at:
            continue
        remaining = c.expires_at - now
        if 0 < remaining.total_seconds() <= 24 * 3600:
            hours_left = max(1, int(remaining.total_seconds() // 3600))
            items.append(
                NotificationItem(
                    id=f"lease-reminder-{c.id}",
                    type="lease_renew_reminder_1d",
                    title="容器即将到期，请及时续租",
                    message=f"容器 {c.name} 约 {hours_left} 小时后到期，可前往“我的容器”续租。",
                    created_at=c.expires_at - timedelta(hours=24),
                    container_id=c.id,
                    container_name=c.name,
                    gpu_ids=c.gpu_ids or "",
                )
            )

    # 3) 我发起的共用申请还在等待他人同意（给申请人提示）
    my_pending = (
        db.query(ContainerModel)
        .filter(
            ContainerModel.user_id == user.id,
            ContainerModel.status == "pending_share_approval",
        )
        .order_by(ContainerModel.created_at.desc())
        .all()
    )
    for c in my_pending:
        payload = _parse_share_payload(c.pending_share_json)
        if not payload:
            continue
        approvers = payload.get("approvers") or []
        approved_cnt = sum(1 for a in approvers if a.get("approved"))
        total_cnt = len(approvers)
        items.append(
            NotificationItem(
                id=f"share-waiting-{c.id}",
                type="share_waiting_for_others",
                title="GPU 共用申请审批中",
                message=f"容器申请 {c.name} 正在等待审批（{approved_cnt}/{total_cnt} 已同意）。",
                created_at=c.created_at or now,
                container_id=c.id,
                container_name=c.name,
                gpu_ids=c.gpu_ids or "",
            )
        )

    items.sort(key=lambda x: x.created_at, reverse=True)
    return NotificationListResponse(unread_count=len(items), items=items)


@router.post("/{container_id}/approve-share")
def approve_share(container_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    c = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
    if not c or c.status != "pending_share_approval":
        raise HTTPException(status_code=404, detail="申请不存在或已处理")
    payload = _parse_share_payload(c.pending_share_json)
    if not payload:
        raise HTTPException(status_code=400, detail="无效的审批数据")

    approvers = payload.get("approvers") or []
    me = next((a for a in approvers if int(a["user_id"]) == user.id), None)
    if not me:
        raise HTTPException(status_code=403, detail="您不是该申请的待审批占用者")

    if me.get("approved"):
        return {"message": "您已同意过"}

    me["approved"] = True
    me["approved_at"] = datetime.now().isoformat()

    c.pending_share_json = json.dumps(payload, ensure_ascii=False)

    if all(bool(a.get("approved")) for a in approvers):
        lease_days = int(payload.get("lease_days", DEFAULT_LEASE_DAYS))
        try:
            _provision_running_container(db, c, lease_days)
        except HTTPException:
            db.rollback()
            raise
        return {"message": "已全部同意，容器已创建"}

    db.commit()
    return {"message": "已记录您的同意"}


@router.post("/{container_id}/reject-share")
def reject_share(container_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    c = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
    if not c or c.status != "pending_share_approval":
        raise HTTPException(status_code=404, detail="申请不存在或已处理")
    payload = _parse_share_payload(c.pending_share_json)
    if not payload:
        raise HTTPException(status_code=400, detail="无效的审批数据")
    approvers = payload.get("approvers") or []
    if not any(int(a["user_id"]) == user.id for a in approvers):
        raise HTTPException(status_code=403, detail="您不是该申请的待审批占用者")

    now = datetime.now()
    c.status = "share_rejected"
    c.pending_share_json = None
    c.stopped_at = now
    c.name = f"{c.name}-rej-{int(now.timestamp())}"
    db.commit()
    return {"message": "已拒绝该共用申请"}


@router.get("/my", response_model=list[ContainerResponse])
def my_containers(user=Depends(get_current_user), db=Depends(get_db)):
    rows = (
        db.query(ContainerModel)
        .filter(
            ContainerModel.user_id == user.id,
            ContainerModel.status != "removed",
        )
        .order_by(ContainerModel.created_at.desc())
        .all()
    )
    return [_response_from_container(db, r, user.username) for r in rows]


@router.delete("/{container_id}")
def delete_container(container_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    from app.container_lifecycle import remove_container_record

    c = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="容器不存在")

    if c.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="无权操作此容器")

    if c.status == "pending_share_approval":
        c.status = "removed"
        c.stopped_at = datetime.now()
        c.name = f"{c.name}-cancel-{int(datetime.now().timestamp())}"
        c.pending_share_json = None
        c.container_id = None
        db.commit()
        return {"message": "已取消待审批申请"}

    if c.status == "removed" and not c.container_id:
        return {"message": "容器已销毁，记录已存档"}

    result = remove_container_record(db, c, "用户主动删除")
    if not result.success:
        raise HTTPException(status_code=500, detail=f"容器销毁失败: {result.error or '未知错误'}")
    return {"message": "容器已成功停止并销毁，记录已存档"}
