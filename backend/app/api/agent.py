import json
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from app.config import AGENT_API_TOKEN, NODE_ID, NODE_NAME, NODE_PUBLIC_HOST, NODE_ROLE, NODE_SERVICE_SCHEME
from app.database import get_db
from app.database_models import ComputeNodeModel, ShareRequestModel
from app.worker_share import create_request, expire_request, master_workspace, reject_unapproved_occupancy, require_current, validated_result, verified_runtime, view, worker_gpu_lock
from app.node_service import active_owners
from app.docker_service import allocate_service_ports, allocate_ssh_port, create_container, finalize_gpu_merge, get_docker_client, is_managed_container, list_managed_containers, merge_container_gpus, remove_container, rollback_gpu_merge, stop_container
from app.node_service import local_inventory
from app.quota_service import worker_master_workspace_data_result, worker_master_workspace_usage_result

router = APIRouter()
security = HTTPBearer(auto_error=False)
_create_lock = worker_gpu_lock
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


class AgentShareRequest(AgentContainerCreate):
    request_id: str = Field(pattern=r"^[a-f0-9]{32,64}$")
    applicant: str
    lease_days: int = Field(ge=1)

    @field_validator("applicant")
    @classmethod
    def validate_applicant(cls, value):
        return AgentContainerCreate.validate_username(value)


@router.post("/share-requests", dependencies=[Depends(require_agent_token)])
def request_share(req: AgentShareRequest, db=Depends(get_db)):
    with _create_lock:
        return create_request(db, req)


@router.get("/share-requests/{request_id}", dependencies=[Depends(require_agent_token)])
def share_status(request_id: str, db=Depends(get_db)):
    with _create_lock:
        db.expire_all()
        row = db.query(ShareRequestModel).filter_by(id=request_id).with_for_update().first()
        if not row:
            raise HTTPException(status_code=404, detail="申请不存在")
        expire_request(db, row)
        if row.state in ("provisioning", "uncertain"):
            return _recover_share(db, row)
        if row.state == "provisioned":
            try:
                result = validated_result(row)
            except HTTPException as exc:
                if exc.status_code == 503:
                    raise
                row.state = "uncertain"
                db.commit()
                return view(row)
            return {**view(row), "ssh_password": result["ssh_password"]}
        return view(row)


@router.delete("/share-requests/{request_id}", dependencies=[Depends(require_agent_token)])
def cancel_share(request_id: str, db=Depends(get_db)):
    with _create_lock:
        db.expire_all()
        row = db.query(ShareRequestModel).filter_by(id=request_id).with_for_update().first()
        if not row:
            raise HTTPException(status_code=404, detail="申请不存在")
        expire_request(db, row)
        if row.state in ("pending", "approved") and not row.provision_result:
            changed = db.query(ShareRequestModel).filter(ShareRequestModel.id == request_id,
                ShareRequestModel.state == row.state, ShareRequestModel.provision_result.is_(None)).update(
                {ShareRequestModel.state: "cancelled"}, synchronize_session=False)
            if changed != 1:
                db.rollback()
                raise HTTPException(status_code=409, detail="申请无法取消")
            db.commit()
            db.refresh(row)
        elif row.state != "cancelled":
            raise HTTPException(status_code=409, detail="申请无法取消")
        return view(row)


def _recover_share(db, row):
    try:
        intent = json.loads(row.provision_result or "null")
        if not isinstance(intent, dict) or not intent.get("ssh_password") or not intent.get("ssh_port"):
            raise ValueError("missing intent")
        runtime = verified_runtime(row, intent)
    except HTTPException as exc:
        if exc.status_code == 503:
            raise
        row.state = "uncertain"
        db.commit()
        return view(row)
    except (ValueError, TypeError):
        row.state = "uncertain"
        db.commit()
        return view(row)
    intent["container_id"] = runtime["container_id"]
    intent["extra_ports"] = runtime["extra_ports"]
    row.provision_result = json.dumps(intent)
    row.state = "provisioned"
    db.commit()
    return {**view(row), "ssh_password": intent["ssh_password"]}


@router.post("/share-requests/{request_id}/provision", dependencies=[Depends(require_agent_token)])
def provision_share(request_id: str, db=Depends(get_db)):
    with _create_lock:
        db.expire_all()
        row = db.query(ShareRequestModel).filter_by(id=request_id).with_for_update().first()
        if not row:
            raise HTTPException(status_code=404, detail="申请不存在")
        expire_request(db, row)
        if row.state in ("provisioning", "uncertain"):
            return _recover_share(db, row)
        if row.state == "provisioned":
            result = validated_result(row)
            return {**view(row), "ssh_password": result["ssh_password"]}
        if row.state != "approved":
            raise HTTPException(status_code=409, detail="共用申请尚未获全部同意")
        payload = require_current(db, row)
        if not all(item["approved"] for item in json.loads(row.approvers)):
            raise HTTPException(status_code=409, detail="共用申请尚未获全部同意")
        if any(item["name"] == payload["name"] for item in list_managed_containers()):
            raise HTTPException(status_code=409, detail="同名容器已存在")
        try:
            ssh_port = allocate_ssh_port()
            extra_ports = allocate_service_ports()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="暂时无法读取端口占用，请稍后重试") from exc
        if not ssh_port or not extra_ports:
            raise HTTPException(status_code=503, detail="暂无可用端口，请稍后重试")
        try:
            workspace_path = master_workspace(payload["username"])
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ssh_password = secrets.token_urlsafe(12)
        intent = {"ssh_port": ssh_port, "public_host": NODE_PUBLIC_HOST,
                  "service_scheme": NODE_SERVICE_SCHEME, "ssh_password": ssh_password,
                  "extra_ports": extra_ports}
        changed = db.query(ShareRequestModel).filter(ShareRequestModel.id == request_id,
            ShareRequestModel.state == "approved", ShareRequestModel.provision_result.is_(None)).update(
            {ShareRequestModel.state: "provisioning", ShareRequestModel.provision_result: json.dumps(intent)},
            synchronize_session=False)
        if changed != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="申请状态已变化")
        db.commit()
        db.refresh(row)
        try:
            container_id, returned_password, extra_ports = create_container(
                payload["name"], payload["username"], payload["gpu_ids"], ssh_port, payload["mem_limit_gb"],
                workspace_path=workspace_path, ssh_password=ssh_password, request_id=request_id,
                extra_ports_map=extra_ports)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="创建结果不确定，申请须人工核查") from exc
        if not container_id or returned_password != ssh_password:
            raise HTTPException(status_code=503, detail="创建结果不确定，申请须人工核查")
        result = {**intent, "container_id": container_id, "extra_ports": extra_ports}
        row.provision_result = json.dumps(result)
        row.state = "provisioned"
        db.commit()
        validated_result(row)
        return {**view(row), "ssh_password": ssh_password}


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
def merge(container_id: str, req: AgentMergeRequest, db=Depends(get_db)):
    with _create_lock:
        reject_unapproved_occupancy(db, req.gpu_ids, container_id, req.old_gpu_ids)
        target = next((item for item in list_managed_containers() if item.get("container_id") == container_id), None)
        if not target or target.get("name") != req.name or target.get("username") != req.username:
            raise HTTPException(status_code=409, detail="目标容器身份不一致")
        _require_managed(container_id)
        try:
            workspace_path = master_workspace(req.username)
            replacement_id = merge_container_gpus(container_id, req.name, req.username, req.old_gpu_ids, req.gpu_ids, req.ssh_port, req.extra_ports, req.ssh_password_hash, req.mem_limit_gb, workspace_path=workspace_path)
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


@router.get("/owners/{node_id}")
def owners_for_worker(node_id: str, credentials: HTTPAuthorizationCredentials = Depends(security), db=Depends(get_db)):
    if NODE_ROLE != "master":
        raise HTTPException(status_code=404, detail="Not Found")
    node = db.query(ComputeNodeModel).filter(ComputeNodeModel.id == node_id, ComputeNodeModel.enabled.is_(True)).first()
    if node_id == NODE_ID or not node or not node.agent_token or not credentials or credentials.scheme.lower() != "bearer" or not secrets.compare_digest(credentials.credentials, node.agent_token):
        raise HTTPException(status_code=401, detail="无效的 Agent 凭据")
    return {"node_id": node_id, "owners": active_owners(db, node_id)}


@router.get("/health", dependencies=[Depends(require_agent_token)])
def health():
    return {"status": "ok", "node_id": NODE_ID, "node_name": NODE_NAME, "role": NODE_ROLE}


@router.get("/workspace-usage/{username}", dependencies=[Depends(require_agent_token)])
def workspace_usage(username: str):
    if not _USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=422, detail="用户名格式无效")
    result = worker_master_workspace_usage_result(username)
    return {"node_id": NODE_ID, "username": username, "usage_bytes": result.usage_bytes, "complete": result.complete,
            "namespace_present": result.namespace_present}


@router.get("/workspace-data", dependencies=[Depends(require_agent_token)])
def workspace_data():
    has_workspace_data, complete = worker_master_workspace_data_result()
    return {"node_id": NODE_ID, "has_workspace_data": has_workspace_data, "complete": complete}


@router.get("/inventory", dependencies=[Depends(require_agent_token)])
def inventory(db=Depends(get_db)):
    with _create_lock:
        return local_inventory(db)


@router.get("/containers", dependencies=[Depends(require_agent_token)])
def containers(db=Depends(get_db)):
    with _create_lock:
        return local_inventory(db)["containers"]


@router.post("/containers", dependencies=[Depends(require_agent_token)])
def create(req: AgentContainerCreate, db=Depends(get_db)):
    with _create_lock:
        reject_unapproved_occupancy(db, req.gpu_ids)
        existing = next((row for row in list_managed_containers() if row["name"] == req.name), None)
        if existing:
            raise HTTPException(status_code=409, detail="同名容器已存在，请核对主节点记录")
        ssh_port = allocate_ssh_port()
        if not ssh_port:
            raise HTTPException(status_code=503, detail="暂无可用 SSH 端口")
        try:
            workspace_path = master_workspace(req.username)
            container_id, ssh_password, extra_ports = create_container(
                req.name,
                req.username,
                req.gpu_ids,
                ssh_port,
                req.mem_limit_gb,
                workspace_path=workspace_path,
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
