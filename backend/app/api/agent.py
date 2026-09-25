import re
import secrets
from threading import RLock

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from app.config import AGENT_API_TOKEN, NODE_ID, NODE_NAME, NODE_PUBLIC_HOST, NODE_ROLE, NODE_SERVICE_SCHEME
from app.database import get_db
from app.docker_service import allocate_ssh_port, create_container, finalize_gpu_merge, get_docker_client, is_managed_container, list_managed_containers, merge_container_gpus, remove_container, rollback_gpu_merge, stop_container
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
        if not isinstance(value, list) or any(not isinstance(gpu_id, int) or isinstance(gpu_id, bool) or gpu_id < 0 for gpu_id in value):
            raise ValueError("GPU ID 必须是非负整数")
        if len(value) != len(set(value)):
            raise ValueError("GPU ID 不能重复")
        return value


def require_agent_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if NODE_ROLE != "worker":
        raise HTTPException(status_code=404, detail="Not Found")
    if not AGENT_API_TOKEN or not credentials or credentials.scheme.lower() != "bearer" or not secrets.compare_digest(credentials.credentials, AGENT_API_TOKEN):
        raise HTTPException(status_code=401, detail="无效的 Agent 凭据")


class AgentMergeRequest(BaseModel):
    name: str
    username: str
    old_gpu_ids: list[int]
    gpu_ids: list[int]
    ssh_port: int = Field(ge=1, le=65535)
    extra_ports: dict[int, int]
    ssh_password_hash: str
    mem_limit_gb: int = Field(ge=1, le=1024)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        return AgentContainerCreate.validate_name(value)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        return AgentContainerCreate.validate_username(value)

    @field_validator("old_gpu_ids", "gpu_ids", mode="before")
    @classmethod
    def validate_gpu_ids(cls, value):
        return AgentContainerCreate.validate_gpu_ids(value)

    @field_validator("ssh_password_hash")
    @classmethod
    def validate_hash(cls, value):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("凭据摘要格式无效")
        return value

    @field_validator("extra_ports")
    @classmethod
    def validate_ports(cls, value):
        if any(isinstance(key, bool) or isinstance(port, bool) or not 1 <= key <= 65535 or not 1 <= port <= 65535 for key, port in value.items()):
            raise ValueError("端口格式无效")
        return value


class AgentMergeAction(BaseModel):
    name: str
    username: str
    old_gpu_ids: list[int]
    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        return AgentContainerCreate.validate_name(value)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        return AgentContainerCreate.validate_username(value)

    @field_validator("old_gpu_ids", mode="before")
    @classmethod
    def validate_gpu_ids(cls, value):
        return AgentContainerCreate.validate_gpu_ids(value)


class AgentMergeFinalize(AgentMergeAction):
    replacement_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-f0-9]{1,64}$")


@router.post("/containers/{container_id}/merge", dependencies=[Depends(require_agent_token)])
def merge(container_id: str, req: AgentMergeRequest):
    with _create_lock:
        _require_managed(container_id)
        try:
            replacement_id = merge_container_gpus(container_id, req.name, req.username, req.old_gpu_ids, req.gpu_ids, req.ssh_port, req.extra_ports, req.ssh_password_hash, req.mem_limit_gb)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"container_id": replacement_id}


@router.post("/containers/{container_id}/merge/rollback", dependencies=[Depends(require_agent_token)])
def rollback_merge(container_id: str, req: AgentMergeAction):
    with _create_lock:
        _require_managed(container_id)
        try:
            rollback_gpu_merge(container_id, req.name, req.username, req.old_gpu_ids)
        except Exception as exc:
            raise HTTPException(status_code=409, detail="原容器未能恢复") from exc
        return {"status": "restored"}


@router.post("/containers/{container_id}/merge/finalize", dependencies=[Depends(require_agent_token)])
def finalize_merge(container_id: str, req: AgentMergeFinalize):
    with _create_lock:
        _require_managed(container_id)
        try:
            finalize_gpu_merge(container_id, req.name, req.replacement_id, req.username, req.old_gpu_ids)
        except Exception as exc:
            raise HTTPException(status_code=409, detail="替代容器未能完成清理") from exc
        return {"status": "finalized"}


def _require_not_merging(container_id: str) -> None:
    try:
        container = get_docker_client().containers.get(container_id)
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if container.name.endswith("-merge-old") or labels.get("compute-graveyard.merge_source"):
            raise HTTPException(status_code=409, detail="容器正在合并，禁止其他操作")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="无法确认容器合并状态")


def _require_managed(container_id: str) -> None:
    managed = is_managed_container(container_id)
    if managed is False:
        raise HTTPException(status_code=403, detail="拒绝操作非本系统管理的容器")


@router.get("/health", dependencies=[Depends(require_agent_token)])
def health():
    return {"status": "ok", "node_id": NODE_ID, "node_name": NODE_NAME, "role": NODE_ROLE}


@router.get("/inventory", dependencies=[Depends(require_agent_token)])
def inventory(db=Depends(get_db)):
    with _create_lock:
        return local_inventory(db)


@router.get("/containers", dependencies=[Depends(require_agent_token)])
def containers(db=Depends(get_db)):
    with _create_lock:
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
    with _create_lock:
        _require_managed(container_id)
        _require_not_merging(container_id)
        if not stop_container(container_id):
            raise HTTPException(status_code=500, detail="停止容器失败")
        return {"status": "stopped", "container_id": container_id}


@router.delete("/containers/{container_id}", dependencies=[Depends(require_agent_token)])
def delete(container_id: str):
    with _create_lock:
        _require_managed(container_id)
        _require_not_merging(container_id)
        if not remove_container(container_id):
            raise HTTPException(status_code=500, detail="删除容器失败")
        return {"status": "removed", "container_id": container_id}
