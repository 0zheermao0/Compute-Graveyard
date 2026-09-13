import re
import secrets
from threading import RLock

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from app.config import AGENT_API_TOKEN, NODE_ID, NODE_NAME, NODE_PUBLIC_HOST, NODE_ROLE, NODE_SERVICE_SCHEME
from app.database import get_db
from app.docker_service import allocate_ssh_port, create_container, is_managed_container, list_managed_containers, remove_container, stop_container
from app.node_service import local_inventory

router = APIRouter()
security = HTTPBearer(auto_error=False)
_create_lock = RLock()
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")


class AgentContainerCreate(BaseModel):
    name: str
    username: str
    gpu_ids: list[int] = Field(default_factory=list)
    mem_limit_gb: int = Field(default=8, ge=1, le=1024)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if not _NAME_RE.fullmatch(value):
            raise ValueError("容器名称格式无效")
        return value

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        if not _USERNAME_RE.fullmatch(value):
            raise ValueError("用户名格式无效")
        return value

    @field_validator("gpu_ids")
    @classmethod
    def validate_gpu_ids(cls, value):
        if any(not isinstance(gpu_id, int) or isinstance(gpu_id, bool) or gpu_id < 0 for gpu_id in value):
            raise ValueError("GPU ID 必须是非负整数")
        if len(value) != len(set(value)):
            raise ValueError("GPU ID 不能重复")
        return value


def require_agent_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if NODE_ROLE != "worker":
        raise HTTPException(status_code=404, detail="Not Found")
    if not AGENT_API_TOKEN or not credentials or credentials.scheme.lower() != "bearer" or not secrets.compare_digest(credentials.credentials, AGENT_API_TOKEN):
        raise HTTPException(status_code=401, detail="无效的 Agent 凭据")


def _require_managed(container_id: str) -> None:
    managed = is_managed_container(container_id)
    if managed is False:
        raise HTTPException(status_code=403, detail="拒绝操作非本系统管理的容器")


@router.get("/health", dependencies=[Depends(require_agent_token)])
def health():
    return {"status": "ok", "node_id": NODE_ID, "node_name": NODE_NAME, "role": NODE_ROLE}


@router.get("/inventory", dependencies=[Depends(require_agent_token)])
def inventory(db=Depends(get_db)):
    return local_inventory(db)


@router.get("/containers", dependencies=[Depends(require_agent_token)])
def containers(db=Depends(get_db)):
    return local_inventory(db)["containers"]


@router.post("/containers", dependencies=[Depends(require_agent_token)])
def create(req: AgentContainerCreate):
    with _create_lock:
        existing = next((row for row in list_managed_containers() if row["name"] == req.name), None)
        if existing:
            raise HTTPException(status_code=409, detail="同名容器已存在，请核对主节点记录")
        ssh_port = allocate_ssh_port()
        if not ssh_port:
            raise HTTPException(status_code=503, detail="暂无可用 SSH 端口")
        try:
            container_id, ssh_password, extra_ports = create_container(
                req.name,
                req.username,
                req.gpu_ids,
                ssh_port,
                req.mem_limit_gb,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "container_id": container_id,
        "ssh_password": ssh_password,
        "ssh_port": ssh_port,
        "extra_ports": extra_ports,
        "public_host": NODE_PUBLIC_HOST,
        "service_scheme": NODE_SERVICE_SCHEME,
    }


@router.post("/containers/{container_id}/stop", dependencies=[Depends(require_agent_token)])
def stop(container_id: str):
    _require_managed(container_id)
    if not stop_container(container_id):
        raise HTTPException(status_code=500, detail="停止容器失败")
    return {"status": "stopped", "container_id": container_id}


@router.delete("/containers/{container_id}", dependencies=[Depends(require_agent_token)])
def delete(container_id: str):
    _require_managed(container_id)
    if not remove_container(container_id):
        raise HTTPException(status_code=500, detail="删除容器失败")
    return {"status": "removed", "container_id": container_id}
