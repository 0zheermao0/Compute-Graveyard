"""管理员 API"""
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_admin
from app.database import get_db, get_setting, set_setting
from app.database_models import UserModel, ContainerModel
from app.docker_service import stop_container, remove_container
from app.models import UserCreate, UserResponse
from app.auth import get_password_hash
from app.config import DEFAULT_CPU_MEM_GB, DEFAULT_GPU_MEM_GB_PER_GPU, DEFAULT_MAX_GPU_SHARING_USERS

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

    # 先清理该用户的所有容器
    containers = db.query(ContainerModel).filter(ContainerModel.user_id == user_id).all()
    for c in containers:
        if c.container_id:
            try:
                stop_container(c.container_id)
                remove_container(c.container_id)
            except:
                pass
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
    if c.container_id:
        try:
            stop_container(c.container_id)
            remove_container(c.container_id)
        except:
            pass
    
    from datetime import datetime
    now = datetime.now()
    c.status = "removed"
    # 如果之前没停过，记录当前时间为停止时间
    if not c.stopped_at:
        c.stopped_at = now
    # 为避免重名冲突，给旧名称加个时间戳后缀
    c.name = f"{c.name}-del-{int(now.timestamp())}"
    c.container_id = None
    db.commit()
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
def get_settings(admin=Depends(get_current_admin)):
    return {
        "cpu_mem_gb": int(get_setting("cpu_mem_gb", str(DEFAULT_CPU_MEM_GB))),
        "gpu_mem_gb_per_gpu": int(get_setting("gpu_mem_gb_per_gpu", str(DEFAULT_GPU_MEM_GB_PER_GPU))),
        "max_gpu_sharing_users": int(get_setting("max_gpu_sharing_users", str(DEFAULT_MAX_GPU_SHARING_USERS))),
    }


class SettingsUpdate(BaseModel):
    cpu_mem_gb: int
    gpu_mem_gb_per_gpu: int
    max_gpu_sharing_users: int


@router.put("/settings")
def update_settings(req: SettingsUpdate, admin=Depends(get_current_admin)):
    if req.cpu_mem_gb < 1:
        raise HTTPException(status_code=400, detail="CPU 内存配额最小 1 GB")
    if req.gpu_mem_gb_per_gpu < 1:
        raise HTTPException(status_code=400, detail="单卡 GPU 内存配额最小 1 GB")
    if req.max_gpu_sharing_users < 1:
        raise HTTPException(status_code=400, detail="单卡最多共用人数至少为 1")
    set_setting("cpu_mem_gb", str(req.cpu_mem_gb))
    set_setting("gpu_mem_gb_per_gpu", str(req.gpu_mem_gb_per_gpu))
    set_setting("max_gpu_sharing_users", str(req.max_gpu_sharing_users))
    return {"message": "配置已保存"}
