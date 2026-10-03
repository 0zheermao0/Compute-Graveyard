import hashlib
import json
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.config import NODE_ID, NODE_NAME, NODE_PUBLIC_HOST
from app.database import get_db
from app.database_models import ContainerModel, PersonalTokenModel, UserModel
from app.models import GPUInfo
from app.node_service import aggregate_inventories

router = APIRouter()
security = HTTPBearer(auto_error=False)


class PersonalTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int = Field(default=30, ge=1, le=365)


class PersonalTokenInfo(BaseModel):
    id: int
    name: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class PersonalTokenCreated(PersonalTokenInfo):
    token: str


class PersonalContainer(BaseModel):
    id: int
    name: str
    container_id: str
    node_id: str
    node_name: str | None
    status: str
    expires_at: datetime
    gpu_ids: str
    gpus: list[GPUInfo]
    access_host: str | None
    ssh_port: int
    ssh_password: str | None
    extra_ports: dict[str, int] | None
    service_scheme: str


def get_personal_user(credentials: HTTPAuthorizationCredentials = Depends(security), db=Depends(get_db)):
    if not credentials or credentials.scheme.lower() != "bearer" or not credentials.credentials.startswith("cgpat_"):
        raise HTTPException(status_code=401, detail="无效的个人 API 凭据")
    digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    row = db.query(PersonalTokenModel).filter(PersonalTokenModel.token_hash == digest).first()
    if not row or row.revoked_at or row.expires_at <= datetime.now():
        raise HTTPException(status_code=401, detail="无效的个人 API 凭据")
    user = db.get(UserModel, row.user_id)
    if not user or (not user.approved and user.role != "admin"):
        raise HTTPException(status_code=401, detail="无效的个人 API 凭据")
    return user


@router.post("/tokens", response_model=PersonalTokenCreated, status_code=201)
def create_personal_token(req: PersonalTokenCreate, response: Response, user=Depends(get_current_user), db=Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    if not user.approved and user.role != "admin":
        raise HTTPException(status_code=403, detail="账号尚未通过审批")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="令牌名称不能为空")
    token = "cgpat_" + secrets.token_urlsafe(32)
    row = PersonalTokenModel(user_id=user.id, name=name, token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=datetime.now() + timedelta(days=req.expires_in_days))
    db.add(row)
    db.commit()
    db.refresh(row)
    return PersonalTokenCreated(id=row.id, name=row.name, created_at=row.created_at, expires_at=row.expires_at, revoked_at=None, token=token)


@router.get("/tokens", response_model=list[PersonalTokenInfo])
def list_personal_tokens(user=Depends(get_current_user), db=Depends(get_db)):
    return db.query(PersonalTokenModel).filter(PersonalTokenModel.user_id == user.id).order_by(PersonalTokenModel.created_at.desc(), PersonalTokenModel.id.desc()).all()


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_personal_token(token_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    row = db.query(PersonalTokenModel).filter(PersonalTokenModel.id == token_id, PersonalTokenModel.user_id == user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="令牌不存在")
    if not row.revoked_at:
        row.revoked_at = datetime.now()
        db.commit()


@router.get("/docs")
def personal_api_docs(user=Depends(get_current_user)):
    return {"authentication": "Create a token with JWT at POST /api/personal/tokens; send the token as Authorization: Bearer <token> to GET /api/personal/containers. The token is shown only once. Use GET /api/personal/tokens to list metadata and DELETE /api/personal/tokens/{token_id} to revoke. Use your current site's origin as API origin; do not put the token in a URL. GPU metrics are node-level snapshots, not per-container measurements; offline or unavailable GPUs have no metrics."}


@router.get("/containers", response_model=list[PersonalContainer])
def personal_containers(response: Response, user=Depends(get_personal_user), db=Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    now = datetime.now()
    rows = db.query(ContainerModel).filter(ContainerModel.user_id == user.id, ContainerModel.status == "running", ContainerModel.expires_at > now, ContainerModel.container_id.isnot(None)).order_by(ContainerModel.created_at.desc()).all()
    if not rows:
        return []
    snapshots = {}
    for item in aggregate_inventories(db):
        if item["online"] and item["inventory"]:
            snapshots[item["node"]["id"]] = {gpu.index: gpu for gpu in (GPUInfo(**value) for value in item["inventory"]["gpus"])}
    result = []
    for row in rows:
        gpu_indices = [int(value) for value in (row.gpu_ids or "").split(",") if value.strip()]
        metrics = snapshots.get(row.node_id or NODE_ID, {})
        result.append(PersonalContainer(
            id=row.id, name=row.name, container_id=row.container_id,
            node_id=row.node_id or NODE_ID, node_name=row.node_name or NODE_NAME,
            status=row.status, expires_at=row.expires_at, gpu_ids=row.gpu_ids or "",
            gpus=[metrics[index] for index in gpu_indices if index in metrics],
            access_host=row.access_host or NODE_PUBLIC_HOST, ssh_port=row.ssh_port,
            ssh_password=row.ssh_password, extra_ports=json.loads(row.extra_ports) if row.extra_ports else None,
            service_scheme=row.service_scheme or "http",
        ))
    return result
