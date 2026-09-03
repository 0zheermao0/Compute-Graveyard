"""管理员 API"""
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_admin
from app.database import get_db
from app.database_models import UserModel, ContainerModel, LeaseRecordModel
from app.docker_service import stop_container
from app.models import UserCreate, UserResponse
from app.auth import get_password_hash
from app.container_lifecycle import remove_container_record
from app.settings_service import SettingsValues, load_settings, save_settings

router = APIRouter()


@router.post("/users", response_model=dict)
def create_user(req: UserCreate, admin=Depends(get_current_admin), db=Depends(get_db)):
    if db.query(UserModel).filter(UserModel.username == req.username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")
    user = UserModel(
        username=req.username,
        hashed_password=get_password_hash(req.password),
        display_name=req.display_name or req.username,
        role="user",
        approved=1,  # 管理员直接创建的用户默认通过
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "display_name": user.display_name or "", "role": user.role}


@router.get("/users", response_model=list)
def list_users(admin=Depends(get_current_admin), db=Depends(get_db)):
    users = db.query(UserModel).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name or "",
            "real_name": getattr(u, "real_name", None) or "",
            "contact_type": getattr(u, "contact_type", None) or "",
            "contact_value": getattr(u, "contact_value", None) or "",
            "approved": bool(getattr(u, "approved", 1)),
            "role": u.role,
            "created_at": u.created_at,
        }
        for u in users
    ]


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
    if c.container_id and stop_container(c.container_id):
        c.status = "stopped"
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
            "ssh_password": r.ssh_password,
            "status": r.status,
            "expires_at": r.expires_at,
            "owner_username": owner.username if owner else "",
            "created_at": r.created_at,
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
