import fcntl
import json
import secrets
import os
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock, local

from fastapi import HTTPException

from app.config import DATA_DIR, DEFAULT_MAX_GPU_SHARING_USERS, MAX_LEASE_DAYS, NODE_ID, NODE_ROLE, USER_DATA_BASE
from app.database import get_setting
from app.database_models import ContainerModel, ShareRequestModel, UserModel
from app.docker_service import get_docker_client, get_gpu_info, list_managed_containers


def _gpu_set(value):
    try:
        values = [int(part) for part in str(value or "").split(",") if part]
        if any(value < 0 for value in values) or len(values) != len(set(values)):
            raise ValueError
        return set(values)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="GPU 占用信息无效") from exc


class WorkerGPULock:
    def __init__(self):
        self._lock = RLock()
        self._state = local()

    def __enter__(self):
        self._lock.acquire()
        try:
            if NODE_ROLE == "worker" and not getattr(self._state, "depth", 0):
                path = Path(DATA_DIR) / "worker-gpu.lock"
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    if os.fstat(fd).st_uid != os.geteuid() or os.fstat(fd).st_mode & 0o077:
                        raise RuntimeError("GPU 锁文件权限不安全")
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._state.fd = fd
            self._state.depth = getattr(self._state, "depth", 0) + 1
            return self
        except BaseException:
            self._lock.release()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self._state.depth -= 1
            if not self._state.depth and hasattr(self._state, "fd"):
                fd = self._state.fd
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                    del self._state.fd
        finally:
            self._lock.release()


worker_gpu_lock = WorkerGPULock()


def master_workspace(username):
    if not username or not username.isascii() or not username[0].islower() or not all(
            char.islower() or char.isdigit() or char == "-" for char in username):
        raise ValueError("用户名无效")
    base = Path(USER_DATA_BASE)
    if not base.is_dir() or base.is_symlink():
        raise RuntimeError("用户工作区存储路径不可用")
    root = base / ".compute-graveyard-master"
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o700)
        (root / ".namespace").touch(mode=0o600)
    if (root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.geteuid() or
            root.stat().st_mode & 0o077 or not (root / ".namespace").is_file() or
            (root / ".namespace").is_symlink()):
        raise RuntimeError("Master 工作区命名空间不安全")
    path = root / username
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise RuntimeError("Master 用户工作区路径不安全")
    path.mkdir(exist_ok=True)
    return str(path)


def reject_unapproved_occupancy(db, gpu_ids, target_id=None, old_gpu_ids=()):
    want = set(gpu_ids)
    if not want:
        return
    rows = db.query(ContainerModel).filter(ContainerModel.node_id == NODE_ID, ContainerModel.status.in_(["running", "merging"])).all()
    if any(_gpu_set(row.gpu_ids) & want for row in rows):
        raise HTTPException(status_code=409, detail="所选 GPU 有 Worker 本地容器占用，须通过共用审批")
    runtime = list_managed_containers()
    targets = [item for item in runtime if item.get("container_id") == target_id]
    if target_id and (len(targets) != 1 or targets[0].get("status") != "running" or
                      _gpu_set(targets[0].get("gpu_ids")) != set(old_gpu_ids)):
        raise HTTPException(status_code=409, detail="目标容器 GPU 状态不一致")
    for item in runtime:
        if _gpu_set(item.get("gpu_ids")) & want and item.get("status") in ("running", "merging"):
            if item.get("container_id") != target_id or _gpu_set(item.get("gpu_ids")) != set(old_gpu_ids):
                raise HTTPException(status_code=409, detail="所选 GPU 已被其他容器占用")


def occupancy(db, gpu_ids, applicant):
    want = set(gpu_ids)
    if not want or not want.issubset({int(gpu["index"]) for gpu in get_gpu_info()}):
        raise HTTPException(status_code=409, detail="所选 GPU 不可用")
    runtime = list_managed_containers()
    rows = db.query(ContainerModel, UserModel).join(UserModel, ContainerModel.user_id == UserModel.id).filter(
        ContainerModel.node_id == NODE_ID, ContainerModel.status.in_(["running", "merging"])).all()
    owners = {c.container_id: (c, u) for c, u in rows if c.container_id}
    snapshot = []
    seen = set()
    for item in runtime:
        if not _gpu_set(item.get("gpu_ids")) & want:
            continue
        if item.get("status") not in ("running", "merging"):
            continue
        pair = owners.get(item.get("container_id"))
        if (not pair or item.get("status") != "running" or
                item.get("name", "").endswith("-merge-old") or
                item.get("name") != pair[0].name or
                item.get("username") != pair[1].username or
                _gpu_set(item.get("gpu_ids")) != _gpu_set(pair[0].gpu_ids) or
                pair[0].status != "running" or pair[0].expires_at <= datetime.now()):
            raise HTTPException(status_code=409, detail="所选 GPU 存在未知或不一致的占用")
        c, u = pair
        seen.add(c.container_id)
        snapshot.append({"container_id": c.container_id, "user_id": u.id, "username": u.username,
                         "name": c.name, "gpu_ids": sorted(_gpu_set(c.gpu_ids))})
    for c, _ in rows:
        if _gpu_set(c.gpu_ids) & want and c.container_id not in seen:
            raise HTTPException(status_code=409, detail="Worker 容器运行状态不一致")
    snapshot.sort(key=lambda item: item["container_id"])
    max_share = max(1, int(get_setting("max_gpu_sharing_users", str(DEFAULT_MAX_GPU_SHARING_USERS))))
    for gpu_id in want:
        users = {item["user_id"] for item in snapshot if gpu_id in item["gpu_ids"]}
        if len(users) >= max_share:
            raise HTTPException(status_code=409, detail="所选 GPU 共用人数已满")
    return snapshot


def payload_for(req, snapshot, approvers):
    return {"applicant": req.applicant, "name": req.name, "gpu_ids": sorted(req.gpu_ids),
            "lease_days": req.lease_days, "username": req.username, "mem_limit_gb": req.mem_limit_gb,
            "occupancy": snapshot,
            "required_approvers": [{"user_id": a["user_id"], "container_ids": a["container_ids"]} for a in approvers]}


def expire_request(db, row):
    if row.expires_at <= datetime.now() and row.state in ("pending", "approved"):
        row.state = "expired"
        db.commit()
    return row.state


def verified_runtime(row, result):
    try:
        payload = json.loads(row.payload)
        matches = [item for item in list_managed_containers(include_request_id=True) if item.get("request_id") == row.id or item.get("name") == payload["name"] or item.get("container_id") == result.get("container_id")]
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="暂时无法读取容器状态") from exc
    if (len(matches) != 1 or matches[0].get("request_id") != row.id or
            matches[0].get("name") != payload["name"] or matches[0].get("username") != payload["username"] or
            matches[0].get("status") != "running" or _gpu_set(matches[0].get("gpu_ids")) != set(payload["gpu_ids"]) or
            matches[0].get("ssh_port") != result.get("ssh_port") or
            not isinstance(result.get("ssh_password"), str) or not result["ssh_password"] or
            not matches[0].get("container_id")):
        raise HTTPException(status_code=409, detail="创建结果不可确认，申请须人工核查")
    if result.get("container_id") and matches[0]["container_id"] != result["container_id"]:
        raise HTTPException(status_code=409, detail="创建结果不可确认，申请须人工核查")
    if "extra_ports" in result and {str(k): v for k, v in matches[0].get("extra_ports", {}).items()} != {str(k): v for k, v in result["extra_ports"].items()}:
        raise HTTPException(status_code=409, detail="创建结果不可确认，申请须人工核查")
    try:
        container = get_docker_client().containers.get(matches[0]["container_id"])
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        env = container.attrs.get("Config", {}).get("Env") or []
        if (container.name != payload["name"] or labels.get("compute-graveyard.managed") != "true" or
                labels.get("compute-graveyard.request_id") != row.id or
                labels.get("compute-graveyard.username") != payload["username"] or
                labels.get("compute-graveyard.gpu_ids") != ",".join(map(str, sorted(payload["gpu_ids"]))) or
                not any(value.startswith("SSH_PASSWORD=") and
                        secrets.compare_digest(value.split("=", 1)[1], result["ssh_password"]) for value in env)):
            raise HTTPException(status_code=409, detail="创建结果不可确认，申请须人工核查")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="暂时无法读取容器状态") from exc
    return matches[0]


def validated_result(row):
    if row.state != "provisioned" or not row.provision_result:
        raise HTTPException(status_code=409, detail="创建结果不可确认")
    try:
        result = json.loads(row.provision_result)
        verified_runtime(row, result)
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="创建结果不可确认，申请须人工核查") from exc


def view(row):
    payload = json.loads(row.payload)
    approvers = json.loads(row.approvers)
    snapshot = payload["occupancy"]
    state = "uncertain" if row.state == "provisioning" and row.expires_at <= datetime.now() else "expired" if row.state in ("pending", "approved") and row.expires_at <= datetime.now() else row.state
    result = json.loads(row.provision_result) if row.state == "provisioned" and row.provision_result else None
    if result:
        result.pop("ssh_password", None)
    return {"request_id": row.id, "state": state, "expires_at": row.expires_at.isoformat(),
            "occupancy": [{"container_id": item["container_id"], "name": item["name"],
                           "username": item["username"], "gpu_ids": item["gpu_ids"]} for item in snapshot],
            "gpu_occupancy_counts": {str(gpu_id): len({item["user_id"] for item in snapshot
                                                       if gpu_id in item["gpu_ids"]}) for gpu_id in payload["gpu_ids"]},
            "approvers": [{"container_ids": a["container_ids"], "approved": a["approved"]} for a in approvers],
            "provision_result": result}


def cancel_request(db, row):
    expire_request(db, row)
    if row.state in ("pending", "approved") and not row.provision_result:
        row.state = "cancelled"
        db.commit()
    elif row.state != "cancelled":
        raise HTTPException(status_code=409, detail="申请无法取消")
    return view(row)


def require_current(db, row):
    if row.expires_at <= datetime.now():
        raise HTTPException(status_code=409, detail="共用申请已过期")
    payload = json.loads(row.payload)
    if occupancy(db, payload["gpu_ids"], payload["applicant"]) != payload["occupancy"]:
        raise HTTPException(status_code=409, detail="GPU 占用已变化，请重新申请")
    approvers = json.loads(row.approvers)
    if [{"user_id": a["user_id"], "container_ids": a["container_ids"]} for a in approvers] != payload["required_approvers"]:
        raise HTTPException(status_code=409, detail="审批人信息已变化")
    return payload


def create_request(db, req):
    existing = db.get(ShareRequestModel, req.request_id)
    if existing:
        expire_request(db, existing)
        immutable = json.loads(existing.payload)
        if any(immutable[key] != value for key, value in {
                "applicant": req.applicant, "name": req.name, "gpu_ids": sorted(req.gpu_ids),
                "lease_days": req.lease_days, "username": req.username,
                "mem_limit_gb": req.mem_limit_gb}.items()):
            raise HTTPException(status_code=409, detail="申请 ID 已被不同内容占用")
        return view(existing)
    if req.lease_days > MAX_LEASE_DAYS or req.username != req.applicant:
        raise HTTPException(status_code=400, detail="申请人或租期无效")
    snapshot = occupancy(db, req.gpu_ids, req.applicant)
    if not snapshot:
        raise HTTPException(status_code=409, detail="没有需要 Worker 审批的占用者")
    approvers = [{"user_id": uid, "container_ids": sorted(item["container_id"] for item in snapshot if item["user_id"] == uid),
                  "approved": False} for uid in sorted({item["user_id"] for item in snapshot})]
    if not approvers:
        raise HTTPException(status_code=409, detail="没有其他 Worker 占用者")
    row = ShareRequestModel(id=req.request_id, payload=json.dumps(payload_for(req, snapshot, approvers)),
                            approvers=json.dumps(approvers), state="pending",
                            expires_at=datetime.now() + timedelta(hours=24))
    db.add(row)
    db.commit()
    return view(row)


def decide(db, row, user, approve):
    expire_request(db, row)
    if row.state != "pending":
        raise HTTPException(status_code=409, detail="申请已处理")
    require_current(db, row)
    approvers = json.loads(row.approvers)
    mine = next((a for a in approvers if a["user_id"] == user.id), None)
    if not mine:
        raise HTTPException(status_code=403, detail="不是该申请的 Worker 占用者")
    if mine["approved"] and approve:
        return view(row)
    if not all(db.query(ContainerModel).filter(ContainerModel.container_id == cid,
            ContainerModel.user_id == user.id, ContainerModel.node_id == NODE_ID,
            ContainerModel.status == "running").first() for cid in mine["container_ids"]):
        raise HTTPException(status_code=409, detail="Worker 容器所有权已变化")
    mine["approved"] = approve
    if not approve:
        row.state = "rejected"
    elif all(a["approved"] for a in approvers):
        row.state = "approved"
    row.approvers = json.dumps(approvers)
    db.commit()
    return view(row)
