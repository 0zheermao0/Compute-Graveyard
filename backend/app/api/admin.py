"""管理员 API"""
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_admin
from app.database import get_db
from app.database_models import UserModel, ContainerModel, LeaseRecordModel, ComputeNodeModel
from app.models import UserCreate, UserResponse
from app.auth import get_password_hash
from app.container_lifecycle import remove_container_record
from app.settings_service import SettingsValues, load_settings, save_settings
from app.config import DEFAULT_DISK_QUOTA_BYTES, NODE_ID, NODE_ROLE
from app.node_service import aggregate_inventories, inventory_for_node, node_response, normalize_public_host, stop_on_node
from app.remote_agent import RemoteAgentError, normalize_agent_base_url
from app.quota_service import quota_status, quota_status_payload, refresh_user_quota

router = APIRouter()


class ComputeNodeRequest(BaseModel):
    id: str
    name: str
    base_url: str = ""
    public_host: str = ""
    agent_token: Optional[str] = None
    enabled: bool = True
    schedulable: bool = True


class ComputeNodeUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    public_host: Optional[str] = None
    agent_token: Optional[str] = None
    enabled: Optional[bool] = None
    schedulable: Optional[bool] = None


def _require_master():
    if NODE_ROLE not in {"standalone", "master"}:
        raise HTTPException(status_code=403, detail="当前节点不提供主节点管理功能")


@router.get("/nodes", response_model=list)
def list_nodes(admin=Depends(get_current_admin), db=Depends(get_db)):
    _require_master()
    return [node_response(node) for node in db.query(ComputeNodeModel).order_by(ComputeNodeModel.id).all()]


@router.post("/nodes")
def create_node(req: ComputeNodeRequest, admin=Depends(get_current_admin), db=Depends(get_db)):
    _require_master()
    node_id = req.id.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", node_id):
        raise HTTPException(status_code=400, detail="节点 ID 格式无效")
    if db.query(ComputeNodeModel).filter(ComputeNodeModel.id == node_id).first():
        raise HTTPException(status_code=400, detail="节点已存在")
    try:
        base_url = "" if node_id == NODE_ID else normalize_agent_base_url(req.base_url)
        public_host = normalize_public_host(req.public_host, required=node_id != NODE_ID)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if node_id != NODE_ID and not req.agent_token:
        raise HTTPException(status_code=400, detail="远程节点必须提供 Agent 令牌")
    node = ComputeNodeModel(
        id=node_id,
        name=req.name.strip() or node_id,
        base_url=base_url,
        public_host=public_host,
        agent_token=req.agent_token or "",
        enabled=req.enabled,
        schedulable=req.schedulable,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node_response(node)


@router.patch("/nodes/{node_id}")
def update_node(node_id: str, req: ComputeNodeUpdate, admin=Depends(get_current_admin), db=Depends(get_db)):
    _require_master()
    node = db.query(ComputeNodeModel).filter(ComputeNodeModel.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    values = _model_values(req)
    try:
        if values.get("base_url") is not None:
            values["base_url"] = "" if node.id == NODE_ID else normalize_agent_base_url(values["base_url"])
        if values.get("public_host") is not None:
            values["public_host"] = normalize_public_host(values["public_host"], required=node.id != NODE_ID)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for field, value in values.items():
        if value is None:
            continue
        if field == "name":
            value = value.strip() or node.id
        setattr(node, field, value)
    if node.id != NODE_ID and (not node.base_url or not node.agent_token or not node.public_host):
        raise HTTPException(status_code=400, detail="远程节点必须提供 Agent 地址、令牌和公开访问地址")
    db.commit()
    db.refresh(node)
    return node_response(node)


@router.delete("/nodes/{node_id}")
def delete_node(node_id: str, admin=Depends(get_current_admin), db=Depends(get_db)):
    _require_master()
    if node_id == NODE_ID:
        raise HTTPException(status_code=400, detail="不能删除本机节点")
    node = db.query(ComputeNodeModel).filter(ComputeNodeModel.id == node_id).first()
    if not node:
        return {"message": "节点不存在"}
    if db.query(ContainerModel).filter(ContainerModel.node_id == node_id, ContainerModel.status != "removed").count():
        raise HTTPException(status_code=400, detail="节点仍有关联容器，不能删除")
    db.delete(node)
    db.commit()
    return {"message": "节点已删除"}


@router.post("/nodes/{node_id}/test")
def test_node(node_id: str, admin=Depends(get_current_admin), db=Depends(get_db)):
    _require_master()
    node = db.query(ComputeNodeModel).filter(ComputeNodeModel.id == node_id).first()
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    try:
        inventory = inventory_for_node(db, node)
    except RemoteAgentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "node": node_response(node), "inventory": inventory}


@router.get("/nodes-inventory", response_model=list)
@router.get("/nodes/inventory", response_model=list)
def nodes_inventory(admin=Depends(get_current_admin), db=Depends(get_db)):
    _require_master()
    return aggregate_inventories(db)


@router.post("/users", response_model=dict)
def create_user(req: UserCreate, admin=Depends(get_current_admin), db=Depends(get_db)):
    username = req.username.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,29}", username):
        raise HTTPException(status_code=400, detail="用户名请使用小写字母、数字或连字符，长度为 2~30 位")
    if db.query(UserModel).filter(UserModel.username == username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")
    user = UserModel(
        username=username,
        hashed_password=get_password_hash(req.password),
        display_name=req.display_name or req.username,
        role="user",
        approved=1,  # 管理员直接创建的用户默认通过
        disk_quota_bytes=DEFAULT_DISK_QUOTA_BYTES,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "display_name": user.display_name or "", "role": user.role}


@router.get("/users", response_model=list)
def list_users(admin=Depends(get_current_admin), db=Depends(get_db)):
    users = db.query(UserModel).all()
    result = []
    for u in users:
        quota = quota_status(u)
        result.append({
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name or "",
            "real_name": getattr(u, "real_name", None) or "",
            "contact_type": getattr(u, "contact_type", None) or "",
            "contact_value": getattr(u, "contact_value", None) or "",
            "approved": bool(getattr(u, "approved", 1)),
            "role": u.role,
            "created_at": u.created_at,
            "disk_quota_bytes": quota.quota_bytes,
            "disk_usage_bytes": quota.usage_bytes,
            "disk_quota_blocked": quota.blocked,
            "disk_quota_exceeded_since": quota.exceeded_since,
            "scan_complete": quota.scan_complete,
            "usage_checked_at": getattr(u, "disk_usage_checked_at", None),
            "quota_bytes": quota.quota_bytes,
            "usage_bytes": quota.usage_bytes,
            "blocked": quota.blocked,
            "quota_blocked": quota.blocked,
            "over_quota_since": quota.exceeded_since,
            "disk_quota_over_since": quota.exceeded_since,
            "over_quota": quota.over_quota,
            "quota_exempt": quota.exempt,
        })
    return result


class DiskQuotaUpdate(BaseModel):
    quota_bytes: Optional[int] = None
    disk_quota_bytes: Optional[int] = None
    quota_gb: Optional[float] = None
    disk_quota_gb: Optional[float] = None
    quota_gib: Optional[float] = None
    disk_quota_gib: Optional[float] = None
    limit_bytes: Optional[int] = None
    limit_gb: Optional[float] = None
    limit_gib: Optional[float] = None


def _requested_quota_bytes(req: DiskQuotaUpdate) -> int:
    values = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    byte_values = [values[key] for key in ("quota_bytes", "disk_quota_bytes", "limit_bytes") if values.get(key) is not None]
    gib_values = [values[key] for key in ("quota_gb", "disk_quota_gb", "quota_gib", "disk_quota_gib", "limit_gb", "limit_gib") if values.get(key) is not None]
    if byte_values and gib_values:
        raise HTTPException(status_code=400, detail="只能指定一种配额单位")
    if len({int(value) for value in byte_values}) > 1:
        raise HTTPException(status_code=400, detail="配额参数不一致")
    if len({str(value) for value in gib_values}) > 1:
        raise HTTPException(status_code=400, detail="配额参数不一致")
    if byte_values:
        quota_bytes = int(byte_values[0])
    elif gib_values:
        try:
            quota_bytes_decimal = Decimal(str(gib_values[0])) * Decimal(1024**3)
        except (InvalidOperation, ValueError):
            raise HTTPException(status_code=400, detail="配额必须是有效数字")
        if not quota_bytes_decimal.is_finite():
            raise HTTPException(status_code=400, detail="配额必须是有效数字")
        quota_bytes = int(quota_bytes_decimal.to_integral_value(rounding=ROUND_HALF_UP))
    else:
        raise HTTPException(status_code=400, detail="缺少配额值")
    if quota_bytes <= 0 or quota_bytes > 2**63 - 1:
        raise HTTPException(status_code=400, detail="配额必须是正整数且不能超过数据库支持范围")
    return quota_bytes


def _find_quota_user(user_id: int, db):
    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


def _quota_response(user, db, refresh: bool = False):
    status = refresh_user_quota(db, user) if refresh else quota_status(user)
    return quota_status_payload(user, status)


@router.get("/quota", response_model=list)
@router.get("/quota/users", response_model=list)
def list_quotas(admin=Depends(get_current_admin), db=Depends(get_db)):
    return [_quota_response(user, db) for user in db.query(UserModel).all()]


@router.get("/users/{user_id}/quota")
@router.get("/quota/users/{user_id}")
def get_user_quota(user_id: int, admin=Depends(get_current_admin), db=Depends(get_db)):
    return _quota_response(_find_quota_user(user_id, db), db)


@router.put("/users/{user_id}/quota")
@router.put("/quota/users/{user_id}")
def update_user_quota(user_id: int, req: DiskQuotaUpdate, admin=Depends(get_current_admin), db=Depends(get_db)):
    user = _find_quota_user(user_id, db)
    new_quota_bytes = _requested_quota_bytes(req)
    if int(user.disk_quota_bytes or 0) != new_quota_bytes:
        user.disk_quota_exceeded_since = None
        user.disk_quota_blocked = False
    user.disk_quota_bytes = new_quota_bytes
    return _quota_response(user, db, refresh=True)


@router.post("/users/{user_id}/quota/refresh")
def refresh_user_quota_for_admin(user_id: int, admin=Depends(get_current_admin), db=Depends(get_db)):
    return _quota_response(_find_quota_user(user_id, db), db, refresh=True)


@router.get("/users/pending", response_model=list)
def list_pending_users(admin=Depends(get_current_admin), db=Depends(get_db)):
    users = db.query(UserModel).filter(UserModel.role == "user", UserModel.approved == 0).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "real_name": getattr(u, "real_name", None) or "",
            "contact_type": getattr(u, "contact_type", None) or "",
            "contact_value": getattr(u, "contact_value", None) or "",
            "created_at": u.created_at,
        }
        for u in users
    ]


@router.post("/users/{user_id}/approve")
def approve_user(user_id: int, admin=Depends(get_current_admin), db=Depends(get_db)):
    u = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    u.approved = 1
    db.commit()
    return {"message": "已通过审批"}


@router.post("/users/{user_id}/reject")
def reject_user(user_id: int, admin=Depends(get_current_admin), db=Depends(get_db)):
    u = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    u.approved = 0
    db.commit()
    return {"message": "已拒绝"}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin=Depends(get_current_admin), db=Depends(get_db)):
    u = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")

    if u.role == "admin":
        # 防止自杀或删除其他管理员（可根据需求调整）
        raise HTTPException(status_code=400, detail="不能在管理后台删除管理员账号")

    containers = db.query(ContainerModel).filter(ContainerModel.user_id == user_id).all()
    for c in containers:
        if c.status != "removed" and c.container_id:
            result = remove_container_record(db, c, "管理员删除用户")
            if not result.success:
                raise HTTPException(status_code=500, detail=f"容器 {c.name} 销毁失败: {result.error or '未知错误'}")
        elif c.status not in {"removed", "pending_share_approval", "share_rejected"}:
            raise HTTPException(status_code=500, detail=f"容器 {c.name} 缺少 Docker ID，无法确认资源已清理")
        db.query(LeaseRecordModel).filter(LeaseRecordModel.container_id == c.id).delete(synchronize_session=False)
        db.delete(c)

    db.delete(u)
    db.commit()
    return {"message": "用户及其关联资源已成功删除"}


@router.post("/containers/{container_id}/force-stop")
def force_stop(container_id: int, admin=Depends(get_current_admin), db=Depends(get_db)):
    c = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="容器不存在")
    if c.container_id and stop_on_node(db, c):
        c.status = "stopped"
        c.stop_reason = "admin"
        from datetime import datetime
        c.stopped_at = datetime.now()
        db.commit()
        return {"message": "已强制停止"}
    raise HTTPException(status_code=500, detail="停止失败")


@router.post("/containers/{container_id}/force-remove")
def force_remove(container_id: int, admin=Depends(get_current_admin), db=Depends(get_db)):
    c = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="容器不存在")
    if c.status == "removed" and not c.container_id:
        return {"message": "容器已清理并存档"}
    result = remove_container_record(db, c, "管理员强制清理")
    if not result.success:
        raise HTTPException(status_code=500, detail=f"容器销毁失败: {result.error or '未知错误'}")
    return {"message": "已成功强制清理容器并存档记录"}


@router.get("/containers", response_model=list)
def list_all_containers(admin=Depends(get_current_admin), db=Depends(get_db)):
    rows = db.query(ContainerModel).all()
    result = []
    for r in rows:
        owner = db.query(UserModel).filter(UserModel.id == r.user_id).first()
        ep = json.loads(r.extra_ports) if r.extra_ports else None
        result.append({
            "id": r.id,
            "name": r.name,
            "container_id": r.container_id,
            "gpu_ids": r.gpu_ids or "",
            "ssh_port": r.ssh_port,
            "extra_ports": ep,
            "status": r.status,
            "stop_reason": getattr(r, "stop_reason", None),
            "expires_at": r.expires_at,
            "owner_username": owner.username if owner else "",
            "created_at": r.created_at,
            "node_id": r.node_id,
            "node_name": r.node_name,
            "access_host": r.access_host,
        })
    return result


# ---- 资源配额设置 ----

@router.get("/settings")
def get_settings(admin=Depends(get_current_admin), db=Depends(get_db)):
    try:
        return load_settings(db)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"系统设置无效: {exc}") from exc


class SettingsUpdate(BaseModel):
    cpu_mem_gb: int
    gpu_mem_gb_per_gpu: int
    max_gpu_sharing_users: int
    idle_gpu_reclaim_enabled: bool
    idle_gpu_util_threshold_percent: int
    idle_gpu_memory_threshold_percent: int
    idle_gpu_duration_hours: int


def _model_values(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


@router.put("/settings")
def update_settings(req: SettingsUpdate, admin=Depends(get_current_admin), db=Depends(get_db)):
    values = SettingsValues(**_model_values(req))
    try:
        return save_settings(db, values)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
