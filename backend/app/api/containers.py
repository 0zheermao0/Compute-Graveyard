"""容器申请 API"""
import json
import hashlib
import logging
import uuid
from datetime import datetime, timedelta
from functools import wraps
from app.api.agent import _create_lock

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.auth import get_current_user
from app.database import get_db
from app.database_models import ContainerModel, ShareRequestModel, UserModel, UserNotificationModel
from app.worker_share import decide as decide_remote_share, require_current as require_remote_share_current, view as remote_share_view
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
    MAX_CONTAINERS_PER_USER,
    DEFAULT_CPU_MEM_GB,
    DEFAULT_GPU_MEM_GB_PER_GPU,
    DEFAULT_MAX_GPU_SHARING_USERS,
    NODE_ID,
    NODE_NAME,
    NODE_ROLE,
    NODE_PUBLIC_HOST,
)
from app.node_service import build_service_url, delete_provisioned_container, get_node, inventory_for_node, provision_on_node, select_node, worker_share_snapshot
from app.remote_agent import RemoteAgentClient, RemoteAgentError
from app.docker_service import finalize_gpu_merge, merge_container_gpus, rollback_gpu_merge
from app.database import get_setting
from app.quota_service import check_user_can_provision

router = APIRouter()
_share_action_lock = _create_lock
logger = logging.getLogger(__name__)


def _synchronized_share_action(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with _share_action_lock:
            return func(*args, **kwargs)
    return wrapper


from app.reputation_service import enforce_lease, gpu_max_lease_days


@router.get("/application-policy")
def application_policy(user=Depends(get_current_user), db=Depends(get_db)):
    return {"gpu_max_lease_days": gpu_max_lease_days(db, user), "cpu_max_lease_days": MAX_LEASE_DAYS}


def _max_gpu_sharing(db) -> int:
    v = int(get_setting("max_gpu_sharing_users", str(DEFAULT_MAX_GPU_SHARING_USERS)))
    return max(1, v)


def _distinct_users_per_gpu_map(db, node_id: str = NODE_ID) -> dict[int, set[int]]:
    from collections import defaultdict

    m = defaultdict(set)
    for c in db.query(ContainerModel).filter(ContainerModel.status.in_(["running", "merging"]), ContainerModel.node_id == node_id).all():
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


def _occupiers_for_gpus(db, gpu_ids: list[int], applicant_id: int, node_id: str = NODE_ID) -> list[tuple[int, str]]:
    """在所选 GPU 上有运行中容器的其他用户（需征求同意），按 user_id 排序。"""
    seen: dict[int, str] = {}
    want = set(gpu_ids)
    for c in db.query(ContainerModel).filter(ContainerModel.status.in_(["running", "merging"]), ContainerModel.node_id == node_id).all():
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


def _remote_share_status(db, c, payload):
    node = get_node(db, c.node_id)
    if not node or not node.enabled:
        raise RemoteAgentError("Worker 节点不可用")
    agent_client = RemoteAgentClient(node.base_url, node.agent_token)
    status = (agent_client.recover_share(payload["request_id"]) if c.status in ("provisioning", "share_uncertain")
              else agent_client.share_status(payload["request_id"]))
    if not isinstance(status, dict) or status.get("request_id") != payload["request_id"]:
        raise RemoteAgentError("Worker 申请身份不一致")
    if status.get("occupancy") != payload["occupancy"]:
        raise RemoteAgentError("Worker 申请占用快照不一致")
    return node, agent_client, status


def _finish_remote_share(db, c, payload, node, result):
    provisioned = result.get("provision_result") if isinstance(result, dict) else None
    if (not isinstance(provisioned, dict) or result.get("request_id") != payload["request_id"]
            or result.get("state") != "provisioned" or result.get("occupancy") != payload["occupancy"]
            or not isinstance(result.get("ssh_password"), str) or not result["ssh_password"]
            or not isinstance(provisioned.get("container_id"), str) or not provisioned["container_id"]
            or not isinstance(provisioned.get("ssh_port"), int) or isinstance(provisioned["ssh_port"], bool)
            or not 1 <= provisioned["ssh_port"] <= 65535
            or not isinstance(provisioned.get("extra_ports"), dict)):
        raise RemoteAgentError("Worker 未返回完整的创建凭据")
    c.container_id = provisioned["container_id"]
    c.ssh_password = result["ssh_password"]
    c.ssh_port = provisioned["ssh_port"]
    c.extra_ports = json.dumps(provisioned["extra_ports"])
    c.access_host = node.public_host or provisioned.get("public_host")
    c.service_scheme = provisioned.get("service_scheme") if provisioned.get("service_scheme") in ("http", "https") else "http"
    user = db.get(UserModel, c.user_id)
    allowed_days = min(payload["lease_days"], gpu_max_lease_days(db, user))
    c.expires_at = datetime.now() + timedelta(days=allowed_days)
    c.pending_share_json = None
    c.status = "running"
    db.commit()


def _remote_applicant_limits(db, c, gpu_ids):
    if c.target_container_id is not None:
        return False
    active = db.query(ContainerModel).filter(
        ContainerModel.user_id == c.user_id,
        ContainerModel.id != c.id,
        ContainerModel.status.in_(["running", "merging", "pending_share_approval", "provisioning", "share_uncertain"]),
        ContainerModel.target_container_id.is_(None),
    ).all()
    if len(active) >= MAX_CONTAINERS_PER_USER:
        return False
    user = db.get(UserModel, c.user_id)
    if not user:
        return False
    running = db.query(ContainerModel).filter(
        ContainerModel.user_id == c.user_id,
        ContainerModel.id != c.id,
        ContainerModel.status.in_(["running", "merging", "provisioning", "share_uncertain"]),
    ).all()
    return sum(len(row.gpu_ids.split(",")) for row in running if row.gpu_ids) + len(gpu_ids) <= user.max_gpus_per_user


def _reconcile_remote_shares(db, user_id):
    rows = db.query(ContainerModel).filter(ContainerModel.user_id == user_id,
        ContainerModel.status.in_(["pending_share_approval", "provisioning", "share_uncertain"])).all()
    for c in rows:
        payload = _parse_share_payload(c.pending_share_json)
        if not payload or not payload.get("request_id"):
            continue
        try:
            node, agent_client, status = _remote_share_status(db, c, payload)
            state = status.get("state")
            if c.status in ("provisioning", "share_uncertain"):
                if state == "provisioned":
                    _finish_remote_share(db, c, payload, node, status)
                elif state == "uncertain":
                    c.status = "share_uncertain"
                    db.commit()
                elif state in ("rejected", "cancelled", "expired"):
                    c.status = "share_rejected"
                    c.stopped_at = datetime.now()
                    c.name = f"{c.name}-invalid-{c.id}"
                    db.commit()
                continue
            if state in ("rejected", "cancelled", "expired"):
                c.status = "share_rejected"
                c.stopped_at = datetime.now()
                c.name = f"{c.name}-invalid-{c.id}"
                db.commit()
                continue
            if state == "uncertain":
                c.status = "share_uncertain"
                db.commit()
                continue
            if state not in ("pending", "approved"):
                continue
            approvers = status.get("approvers")
            if (not isinstance(approvers, list) or not approvers or
                    any(not isinstance(a, dict) or not isinstance(a.get("approved"), bool) or
                        not isinstance(a.get("container_ids"), list) for a in approvers)):
                continue
            groups = {}
            for item in payload["occupancy"]:
                groups.setdefault(item["username"], set()).add(item["container_id"])
            if {frozenset(a["container_ids"]) for a in approvers} != {frozenset(ids) for ids in groups.values()}:
                continue
            payload["approvers"] = [{"username": name, "approved": next(a["approved"] for a in approvers
                                    if set(a["container_ids"]) == ids)} for name, ids in groups.items()]
            c.pending_share_json = json.dumps(payload)
            db.commit()
            if state != "approved" or not all(a.get("approved") is True for a in approvers):
                continue
            user = db.get(UserModel, c.user_id)
            if (not user or not node.enabled or not node.schedulable
                    or not _remote_applicant_limits(db, c, payload["gpu_ids"])
                    or not check_user_can_provision(db, user, commit=False).allowed):
                continue
            inventory = inventory_for_node(db, node)
            if worker_share_snapshot(db, node.id, inventory, payload["gpu_ids"]) != payload["occupancy"]:
                continue
            try:
                enforce_lease(db, user, payload["lease_days"])
            except HTTPException:
                continue
            c.status = "provisioning"
            db.commit()
            result = agent_client.provision_share(payload["request_id"])
            _finish_remote_share(db, c, payload, node, result)
        except (RemoteAgentError, ValueError, KeyError, TypeError, RuntimeError, SQLAlchemyError):
            db.rollback()
            logger.warning("Worker 共用申请 %s 暂不可调和", c.id)


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
                if "user_id" not in a:
                    continue
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

    access_host = c.access_host or NODE_PUBLIC_HOST
    service_scheme = getattr(c, "service_scheme", None) or "http"
    service_urls = None
    if c.status == "running" and access_host and ep_int:
        service_urls = {str(port): build_service_url(service_scheme, access_host, host_port) for port, host_port in ep_int.items()}
    return ContainerResponse(
        id=c.id,
        name=c.name,
        container_id=c.container_id,
        gpu_ids=c.gpu_ids or "",
        ssh_port=c.ssh_port or 0,
        ssh_password=c.ssh_password,
        extra_ports=ep_int,
        status=c.status,
        stop_reason=getattr(c, "stop_reason", None),
        expires_at=c.expires_at,
        owner_username=uname or "",
        created_at=c.created_at,
        share_approvers=share_list,
        pending_lease_days=pending_days,
        node_id=c.node_id or NODE_ID,
        node_name=c.node_name or NODE_NAME,
        access_host=access_host,
        service_scheme=service_scheme,
        ssh_host=access_host,
        ssh_url=f"ssh://{access_host}:{c.ssh_port}" if c.ssh_port else None,
        service_urls=service_urls,
        target_container_id=c.target_container_id,
    )


def _merge_target(db, user_id: int, target_id: int, node_id: str | None = None) -> ContainerModel:
    target = db.query(ContainerModel).filter(ContainerModel.id == target_id).first()
    if not target or target.user_id != user_id or target.status != "running" or not target.container_id or not target.ssh_password or target.expires_at <= datetime.now():
        raise HTTPException(status_code=400, detail="目标容器不存在、已过期或不可合并")
    if "old_id" in (_parse_share_payload(target.pending_share_json) or {}):
        raise HTTPException(status_code=409, detail="目标容器尚有未完成的合并清理")
    if node_id is not None and (target.node_id or NODE_ID) != node_id:
        raise HTTPException(status_code=400, detail="新增 GPU 必须与目标容器位于同一节点")
    return target


def _merge_action(node, action: str, old_id: str, data: dict):
    if node.id == NODE_ID:
        if action == "merge":
            return {"container_id": merge_container_gpus(old_id, **data)}
        if action == "rollback":
            return rollback_gpu_merge(old_id, data["name"], data["username"], data["old_gpu_ids"])
        return finalize_gpu_merge(old_id, data["name"], data["replacement_id"], data["username"], data["old_gpu_ids"])
    agent = RemoteAgentClient(node.base_url, node.agent_token)
    if action == "merge":
        if data.get("shrink"):
            return agent.shrink_container(old_id, {key: value for key, value in data.items() if key != "shrink"})
        return agent.merge_container(old_id, data)
    if action == "rollback":
        return agent.rollback_merge(old_id, data)
    return agent.finalize_merge(old_id, data)


def _close_merge_request(pending: ContainerModel) -> None:
    pending.status = "removed"
    pending.pending_share_json = None
    pending.container_id = None
    pending.stopped_at = datetime.now()
    pending.name = f"{pending.name}-done-{pending.id}"


def _perform_merge(db, target: ContainerModel, additional: list[int], user: UserModel, pending: ContainerModel | None = None, previous_approval: str | None = None, keep_gpus: list[int] | None = None) -> None:
    node = get_node(db, target.node_id or NODE_ID)
    if not node:
        raise HTTPException(status_code=400, detail="目标节点不存在")
    old_gpus = [int(x) for x in target.gpu_ids.split(",")] if target.gpu_ids else []
    shrinking = keep_gpus is not None
    if not shrinking:
        enforce_lease(db, user, 1, expires_at=target.expires_at)
    new_gpus = sorted(set(keep_gpus)) if shrinking else sorted(set(old_gpus) | set(additional))
    if shrinking and (not new_gpus or not set(new_gpus) < set(old_gpus)):
        raise HTTPException(status_code=400, detail="缩卡必须保留至少一张卡")
    old_id = target.container_id
    action = {"name": target.name, "username": user.username, "old_gpu_ids": old_gpus}
    target.status = "merging"
    target.gpu_idle_low_since = None
    target.gpu_idle_last_sample_at = None
    target.gpu_idle_stage_mask = 0
    target.gpu_idle_warned_at = None
    target.gpu_idle_memory_snapshot = None
    target.gpu_idle_cards_json = None
    target.gpu_ids = ",".join(map(str, old_gpus if shrinking else new_gpus))
    target.pending_share_json = json.dumps({"old_id": old_id, "old_gpus": old_gpus, "pending_id": pending.id if pending else None, "previous_approval": previous_approval})
    db.commit()
    try:
        gpu_mem_gb = int(get_setting("gpu_mem_gb_per_gpu", str(DEFAULT_GPU_MEM_GB_PER_GPU)))
        data = {**action, "gpu_ids": new_gpus, "ssh_port": target.ssh_port,
                "extra_ports": json.loads(target.extra_ports) if target.extra_ports else {},
                "ssh_password_hash": hashlib.sha256(target.ssh_password.encode()).hexdigest(), "mem_limit_gb": gpu_mem_gb * len(new_gpus)}
        if shrinking:
            data["shrink"] = True
        result = _merge_action(node, "merge", old_id, data)
        replacement_id = result["container_id"]
        target.container_id = replacement_id
        target.gpu_ids = ",".join(map(str, old_gpus if shrinking else new_gpus))
        target.pending_share_json = json.dumps({"old_id": old_id, "old_gpus": old_gpus, "pending_id": pending.id if pending else None, "phase": "finalize", "replacement_id": replacement_id, "shrink_gpus": new_gpus if shrinking else None})
        if pending:
            _close_merge_request(pending)
        db.commit()
    except Exception as exc:
        db.rollback()
        try:
            _merge_action(node, "rollback", old_id, action)
            db.refresh(target)
            target.status = "running"
            target.gpu_ids = ",".join(map(str, old_gpus))
            target.pending_share_json = None
            if pending:
                db.refresh(pending)
                pending.pending_share_json = previous_approval
            db.commit()
        except Exception:
            db.rollback()
            raise HTTPException(status_code=500, detail="合并未能恢复，请联系管理员；原容器数据已保留") from exc
        raise HTTPException(status_code=500, detail="合并失败，原容器已恢复") from exc
    try:
        _merge_action(node, "finalize", old_id, {**action, "replacement_id": replacement_id})
        from app.gpu_history import resize_container_history
        resize_container_history(db, node.id, old_id, replacement_id, new_gpus)
        if shrinking:
            _finish_idle_shrink(db, target, new_gpus, old_id)
        target.pending_share_json = None
        target.status = "running"
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="新容器已运行但清理尚未完成，系统将重试恢复") from exc


def _finish_idle_shrink(db, target, new_gpus, old_id):
    from app.reputation_service import record_configured_event
    record_configured_event(db, target.user_id, f"gpu-idle-shrunk-{target.id}-{old_id}", "idle_shrink", target.id)
    removed = sorted({int(x) for x in target.gpu_ids.split(",")} - set(new_gpus))
    target.gpu_ids = ",".join(map(str, new_gpus))
    key = f"gpu-idle-shrunk-{target.id}-{old_id}"
    if not db.query(UserNotificationModel).filter(UserNotificationModel.event_key == key).first():
        db.add(UserNotificationModel(user_id=target.user_id, event_key=key,
            type="gpu_idle_shrunk", title="长期闲置 GPU 已自动缩减",
            message=f"容器 {target.name} 已释放 GPU {removed}，保留 GPU {new_gpus}。容器已重建运行，文件层和 workspace 保留，原进程不保留。",
            container_id=target.id, container_name=target.name, created_at=datetime.now()))


def recover_incomplete_merges(db) -> None:
    for target in db.query(ContainerModel).filter(ContainerModel.pending_share_json.isnot(None), ContainerModel.status.in_(["merging", "running"])).all():
        payload = _parse_share_payload(target.pending_share_json)
        if not payload or "old_id" not in payload:
            logger.error("容器 %s 合并恢复日志无效", target.id)
            continue
        node = get_node(db, target.node_id or NODE_ID)
        user = db.query(UserModel).filter(UserModel.id == target.user_id).first()
        if not node or not user:
            logger.error("容器 %s 合并恢复缺少节点或用户", target.id)
            continue
        action = {"name": target.name, "username": user.username, "old_gpu_ids": payload["old_gpus"]}
        pending = db.query(ContainerModel).filter(ContainerModel.id == payload["pending_id"]).first() if payload.get("pending_id") else None
        try:
            if payload.get("phase") == "finalize" or target.status == "running":
                _merge_action(node, "finalize", payload["old_id"], {**action, "replacement_id": payload.get("replacement_id") or target.container_id})
                from app.gpu_history import resize_container_history
                kept = payload.get("shrink_gpus") or [int(part) for part in target.gpu_ids.split(",") if part]
                resize_container_history(db, node.id, payload["old_id"], payload.get("replacement_id") or target.container_id, kept)
                if payload.get("shrink_gpus"):
                    _finish_idle_shrink(db, target, payload["shrink_gpus"], payload["old_id"])
                if pending and pending.status == "pending_share_approval":
                    _close_merge_request(pending)
            else:
                _merge_action(node, "rollback", payload["old_id"], action)
                target.gpu_ids = ",".join(map(str, payload["old_gpus"]))
                if pending and pending.status == "pending_share_approval" and payload.get("previous_approval"):
                    pending.pending_share_json = payload["previous_approval"]
            target.status = "running"
            target.pending_share_json = None
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("容器 %s 合并恢复失败，保留待恢复状态", target.id)


def _provision_running_container(db, c: ContainerModel, lease_days: int, previous_approval: str | None = None) -> None:
    """由待审批记录实际创建 Docker 容器并改为 running。"""
    user = db.query(UserModel).filter(UserModel.id == c.user_id).first()
    if not user:
        raise HTTPException(status_code=500, detail="用户不存在")

    quota = check_user_can_provision(db, user, commit=False)
    if not quota.allowed:
        detail = "暂时无法确认工作区容量，请稍后重试" if not quota.scan_complete else "工作区已超过磁盘配额，请清理文件后再申请容器"
        raise HTTPException(status_code=400, detail=detail)

    gpu_ids = [int(x) for x in c.gpu_ids.split(",")] if c.gpu_ids else []
    if c.target_container_id is None:
        enforce_lease(db, user, lease_days, gpu=bool(gpu_ids))
    if c.target_container_id is not None:
        target = _merge_target(db, c.user_id, c.target_container_id, c.node_id or NODE_ID)
        if set(gpu_ids) & {int(x) for x in target.gpu_ids.split(",") if x}:
            raise HTTPException(status_code=400, detail="目标容器已占用所申请的 GPU")
    else:
        target = None

    my_running = db.query(ContainerModel).filter(
        ContainerModel.user_id == c.user_id,
        ContainerModel.status.in_(["running", "merging", "provisioning", "share_uncertain"]),
    ).all()
    total_gpus = sum(len(item.gpu_ids.split(",")) for item in my_running if item.gpu_ids)
    if total_gpus + len(gpu_ids) > user.max_gpus_per_user:
        raise HTTPException(status_code=400, detail=f"每人最多使用 {user.max_gpus_per_user} 块 GPU")

    users_per_gpu = _distinct_users_per_gpu_map(db, c.node_id or NODE_ID)
    mx = _max_gpu_sharing(db)
    if not _capacity_ok(gpu_ids, c.user_id, mx, users_per_gpu):
        raise HTTPException(status_code=400, detail="审批完成时 GPU 已无可用共用名额，请申请人重新申请")
    current_occupiers = {uid for uid, _ in _occupiers_for_gpus(db, gpu_ids, c.user_id, c.node_id or NODE_ID)}
    approved = {int(a["user_id"]) for a in (_parse_share_payload(c.pending_share_json) or {}).get("approvers", []) if a.get("approved")}
    if not current_occupiers.issubset(approved):
        raise HTTPException(status_code=400, detail="新增 GPU 占用者尚未同意，请重新申请")
    if target:
        _perform_merge(db, target, gpu_ids, user, pending=c, previous_approval=previous_approval)
        return

    node = get_node(db, c.node_id or NODE_ID)
    if not node:
        raise HTTPException(status_code=500, detail="目标节点不存在")

    if gpu_ids:
        gpu_mem_gb_per_gpu = int(get_setting("gpu_mem_gb_per_gpu", str(DEFAULT_GPU_MEM_GB_PER_GPU)))
        mem_limit_gb = gpu_mem_gb_per_gpu * len(gpu_ids)
    else:
        mem_limit_gb = int(get_setting("cpu_mem_gb", str(DEFAULT_CPU_MEM_GB)))

    prefix = "labcpu" if not gpu_ids else "labgpu"
    safe_name = f"{prefix}-{user.username}-{datetime.now().strftime('%Y%m%d%H%M')}-{uuid.uuid4().hex[:8]}"

    enforce_lease(db, user, lease_days, gpu=bool(gpu_ids))
    try:
        provisioned = provision_on_node(node, safe_name, user.username, gpu_ids, mem_limit_gb)
    except (RuntimeError, RemoteAgentError) as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    extra_ports = provisioned.get("extra_ports") or {}
    extra_ports_json = json.dumps({str(k): v for k, v in extra_ports.items()}) if extra_ports else None
    expires_at = datetime.now() + timedelta(days=lease_days)

    c.name = safe_name
    c.container_id = provisioned.get("container_id")
    c.ssh_port = int(provisioned.get("ssh_port") or 0)
    c.ssh_password = provisioned.get("ssh_password")
    c.access_host = provisioned.get("access_host") or node.public_host
    c.service_scheme = provisioned.get("service_scheme") or "http"
    c.extra_ports = extra_ports_json
    c.status = "running"
    c.pending_share_json = None
    c.expires_at = expires_at
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_provisioned_container(node, str(provisioned["container_id"]))
        raise


@router.post("/apply", response_model=ContainerApplyResult)
@_synchronized_share_action
def apply_container(req: ContainerApplyRequest, user=Depends(get_current_user), db=Depends(get_db)):
    if user.role not in ("user", "admin"):
        raise HTTPException(status_code=403, detail="无权限申请")

    quota = check_user_can_provision(db, user)
    if not quota.allowed:
        detail = "暂时无法确认工作区容量，请稍后重试" if not quota.scan_complete else "工作区已超过磁盘配额，请清理文件后再申请容器"
        raise HTTPException(status_code=400, detail=detail)

    if req.target_container_id is None:
        enforce_lease(db, user, req.lease_days, gpu=not req.cpu_only)

    target = None
    if req.target_container_id is not None:
        if req.cpu_only:
            raise HTTPException(status_code=400, detail="合并 GPU 不能选择纯 CPU 模式")
        target = _merge_target(db, user.id, req.target_container_id)
        enforce_lease(db, user, 1, expires_at=target.expires_at)
        if req.placement_mode == "local" and (target.node_id or NODE_ID) != NODE_ID:
            raise HTTPException(status_code=400, detail="新增 GPU 必须与目标容器位于同一节点")
        if req.node_id and req.node_id != (target.node_id or NODE_ID):
            raise HTTPException(status_code=400, detail="新增 GPU 必须与目标容器位于同一节点")
        if db.query(ContainerModel).filter(ContainerModel.target_container_id == target.id, ContainerModel.status == "pending_share_approval").count():
            raise HTTPException(status_code=400, detail="目标容器已有待审批的合并申请")

    my_count = db.query(ContainerModel).filter(
        ContainerModel.user_id == user.id,
        ContainerModel.status.in_(["running", "merging", "pending_share_approval"]),
        ContainerModel.target_container_id.is_(None),
    ).count()
    if not target and my_count >= MAX_CONTAINERS_PER_USER:
        raise HTTPException(status_code=400, detail=f"每人最多同时有 {MAX_CONTAINERS_PER_USER} 个运行中或待审批的容器")

    gpu_ids = [] if req.cpu_only else (req.gpu_ids or [])
    if not req.cpu_only and not gpu_ids:
        raise HTTPException(status_code=400, detail="请选择 GPU 或勾选纯 CPU 容器")

    if target and set(gpu_ids) & set(int(x) for x in target.gpu_ids.split(",") if x):
        raise HTTPException(status_code=400, detail="只能选择目标容器尚未占用的 GPU")

    if not req.cpu_only and gpu_ids:
        my_running = db.query(ContainerModel).filter(
            ContainerModel.user_id == user.id,
            ContainerModel.status.in_(["running", "merging", "provisioning", "share_uncertain"]),
        ).all()
        total_gpus = sum(len(c.gpu_ids.split(",")) for c in my_running if c.gpu_ids)
        if total_gpus + len(gpu_ids) > user.max_gpus_per_user:
            raise HTTPException(status_code=400, detail=f"每人最多使用 {user.max_gpus_per_user} 块 GPU")

    mx = _max_gpu_sharing(db)
    try:
        node, inventory = select_node(
            db,
            "specific" if target else req.placement_mode,
            (target.node_id or NODE_ID) if target else req.node_id,
            gpu_ids,
            req.cpu_only,
            applicant_id=user.id,
            max_share=mx,
            allow_worker_share=not target and req.placement_mode == "specific" and not req.cpu_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RemoteAgentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if node.id != NODE_ID and req.placement_mode == "specific" and not target and gpu_ids:
        try:
            snapshot = worker_share_snapshot(db, node.id, inventory, gpu_ids)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if snapshot:
            request_id = uuid.uuid4().hex
            name = f"labgpu-{user.username}-{datetime.now().strftime('%Y%m%d%H%M')}-{uuid.uuid4().hex[:8]}"
            memory = int(get_setting("gpu_mem_gb_per_gpu", str(DEFAULT_GPU_MEM_GB_PER_GPU))) * len(gpu_ids)
            agent_client = RemoteAgentClient(node.base_url, node.agent_token)
            try:
                answer = agent_client.request_share({"request_id": request_id, "applicant": user.username,
                    "username": user.username, "name": name, "gpu_ids": sorted(gpu_ids),
                    "lease_days": req.lease_days, "mem_limit_gb": memory})
                if (not isinstance(answer, dict) or answer.get("request_id") != request_id
                        or answer.get("state") != "pending" or answer.get("occupancy") != snapshot):
                    raise RemoteAgentError("Worker 返回的申请快照不一致")
                fresh = inventory_for_node(db, node)
                if worker_share_snapshot(db, node.id, fresh, gpu_ids) != snapshot:
                    raise RemoteAgentError("Worker 占用状态已变化")
                payload = {"request_id": request_id, "occupancy": snapshot, "gpu_ids": sorted(gpu_ids),
                           "lease_days": req.lease_days, "approvers": []}
                c = ContainerModel(container_id=f"pending-{request_id}", name=name, user_id=user.id,
                    node_id=node.id, node_name=node.name, access_host=node.public_host,
                    gpu_ids=",".join(map(str, sorted(gpu_ids))), ssh_port=0, status="pending_share_approval",
                    expires_at=datetime.now() + timedelta(days=3650), pending_share_json=json.dumps(payload))
                db.add(c)
                db.commit()
                db.refresh(c)
            except (RemoteAgentError, ValueError, KeyError, TypeError):
                db.rollback()
                try:
                    agent_client.cancel_share(request_id)
                except RemoteAgentError:
                    logger.exception("Worker 共用申请 %s 取消状态不确定", request_id)
                raise HTTPException(status_code=502, detail="Worker 共用申请未能确认，请稍后重试")
            except Exception:
                db.rollback()
                try:
                    agent_client.cancel_share(request_id)
                except RemoteAgentError:
                    logger.exception("Worker 共用申请 %s 取消状态不确定", request_id)
                raise
            return ContainerApplyResult(container=_response_from_container(db, c, user.username),
                pending_share_approval=True, message="已向 Worker 占用者发起共用申请")

    users_per_gpu = _distinct_users_per_gpu_map(db, node.id)
    if not _capacity_ok(gpu_ids, user.id, mx, users_per_gpu):
        raise HTTPException(status_code=400, detail="所选 GPU 已达到共用人数上限，请稍后再试或选择其他卡")

    occupiers = _occupiers_for_gpus(db, gpu_ids, user.id, node.id)

    if occupiers:
        approvers = [{"user_id": uid, "username": uname, "approved": False, "approved_at": None} for uid, uname in occupiers]
        occupancy = db.query(ContainerModel).filter(
            ContainerModel.node_id == node.id, ContainerModel.status.in_(["running", "merging"]),
            ContainerModel.user_id.in_([uid for uid, _ in occupiers])).all()
        payload = {"lease_days": req.lease_days, "approvers": approvers,
                   "occupancy": [{"id": row.id} for row in occupancy
                                 if {int(value) for value in (row.gpu_ids or "").split(",") if value.strip()} & set(gpu_ids)]}
        pend_name = f"labgpu-{user.username}-pend-{datetime.now().strftime('%Y%m%d%H%M')}-{uuid.uuid4().hex[:8]}"
        pending_cid = f"pending-{uuid.uuid4().hex}"
        far_expires = datetime.now() + timedelta(days=3650)
        c = ContainerModel(
            container_id=pending_cid,
            name=pend_name,
            user_id=user.id,
            node_id=node.id,
            node_name=node.name,
            access_host=node.public_host or NODE_PUBLIC_HOST,
            gpu_ids=",".join(map(str, sorted(gpu_ids))),
            ssh_port=0,
            ssh_password=None,
            extra_ports=None,
            status="pending_share_approval",
            expires_at=far_expires,
            pending_share_json=json.dumps(payload, ensure_ascii=False),
            target_container_id=target.id if target else None,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        return ContainerApplyResult(
            container=_response_from_container(db, c, user.username),
            pending_share_approval=True,
            message="新增 GPU 共用申请已发起，全部同意后合并到原容器" if target else "所选 GPU 上有其他同学的容器，已向对方发起共用申请，全部同意后自动创建",
        )

    if target:
        _merge_target(db, user.id, target.id, node.id)
        _perform_merge(db, target, gpu_ids, user)
        return ContainerApplyResult(container=_response_from_container(db, target, user.username), message="GPU 已合并到原容器")

    expires_at = datetime.now() + timedelta(days=req.lease_days)
    prefix = "labcpu" if req.cpu_only else "labgpu"
    container_name = f"{prefix}-{user.username}-{datetime.now().strftime('%Y%m%d%H%M')}-{uuid.uuid4().hex[:8]}"

    if gpu_ids:
        gpu_mem_gb_per_gpu = int(get_setting("gpu_mem_gb_per_gpu", str(DEFAULT_GPU_MEM_GB_PER_GPU)))
        mem_limit_gb = gpu_mem_gb_per_gpu * len(gpu_ids)
    else:
        mem_limit_gb = int(get_setting("cpu_mem_gb", str(DEFAULT_CPU_MEM_GB)))

    enforce_lease(db, user, req.lease_days, gpu=bool(gpu_ids))
    try:
        provisioned = provision_on_node(node, container_name, user.username, gpu_ids, mem_limit_gb)
    except (RuntimeError, RemoteAgentError) as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    extra_ports = provisioned.get("extra_ports") or {}
    extra_ports_json = json.dumps({str(k): v for k, v in extra_ports.items()}) if extra_ports else None
    c = ContainerModel(
        container_id=provisioned.get("container_id"),
        name=container_name,
        user_id=user.id,
        node_id=node.id,
        node_name=node.name,
        access_host=provisioned.get("access_host") or node.public_host or NODE_PUBLIC_HOST,
        service_scheme=provisioned.get("service_scheme") or "http",
        gpu_ids=",".join(map(str, sorted(gpu_ids))) if gpu_ids else "",
        ssh_port=int(provisioned.get("ssh_port") or 0),
        ssh_password=provisioned.get("ssh_password"),
        extra_ports=extra_ports_json,
        status="running",
        expires_at=expires_at,
        pending_share_json=None,
    )
    db.add(c)
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_provisioned_container(node, str(provisioned["container_id"]))
        raise
    db.refresh(c)

    return ContainerApplyResult(
        container=_response_from_container(db, c, user.username),
        pending_share_approval=False,
        message=None,
    )


def _worker_share_only():
    if NODE_ROLE != "worker":
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/remote-share-requests")
def remote_share_requests(user=Depends(get_current_user), db=Depends(get_db)):
    _worker_share_only()
    result = []
    for row in db.query(ShareRequestModel).filter(ShareRequestModel.state == "pending").all():
        if any(a["user_id"] == user.id and not a["approved"] for a in json.loads(row.approvers)):
            try:
                require_remote_share_current(db, row)
            except HTTPException:
                continue
            result.append({**remote_share_view(row), "applicant": json.loads(row.payload)["applicant"],
                           "gpu_ids": json.loads(row.payload)["gpu_ids"]})
    return result


@router.post("/remote-share-requests/{request_id}/approve")
@_synchronized_share_action
def approve_remote_share(request_id: str, user=Depends(get_current_user), db=Depends(get_db)):
    _worker_share_only()
    row = db.get(ShareRequestModel, request_id)
    if not row:
        raise HTTPException(status_code=404, detail="申请不存在")
    return decide_remote_share(db, row, user, True)


@router.post("/remote-share-requests/{request_id}/reject")
@_synchronized_share_action
def reject_remote_share(request_id: str, user=Depends(get_current_user), db=Depends(get_db)):
    _worker_share_only()
    row = db.get(ShareRequestModel, request_id)
    if not row:
        raise HTTPException(status_code=404, detail="申请不存在")
    return decide_remote_share(db, row, user, False)


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
        ids = {int(a["user_id"]) for a in payload.get("approvers") or [] if "user_id" in a}
        if user.id not in ids:
            continue
        mine = next((a for a in payload["approvers"] if int(a["user_id"]) == user.id), None)
        if mine and mine.get("approved"):
            continue
        owner = db.query(UserModel).filter(UserModel.id == c.user_id).first()
        result.append(_response_from_container(db, c, owner.username if owner else ""))
    return result


def pending_share_approver_ids(payload):
    return {int(a["user_id"]) for a in payload.get("approvers", []) if "user_id" in a and not a.get("approved")}


def lease_reminder_active(container, now):
    return bool(container.expires_at and 0 < (container.expires_at - now).total_seconds() <= 24 * 3600)


def remote_share_approver_ids(db, row):
    try:
        approvers = json.loads(row.approvers)
        ids = {a["user_id"] for a in approvers if not a["approved"]}
        if not ids:
            return set()
        require_remote_share_current(db, row)
        return ids
    except (HTTPException, ValueError, TypeError, KeyError):
        return set()


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    event = db.query(UserNotificationModel).filter(
        UserNotificationModel.id == notification_id, UserNotificationModel.user_id == user.id,
    ).first()
    if event is None:
        raise HTTPException(status_code=404, detail="通知不存在")
    if event.read_at is None:
        event.read_at = datetime.now()
        db.commit()
    return {"id": event.id, "read_at": event.read_at}


@router.get("/notifications", response_model=NotificationListResponse)
def list_notifications(user=Depends(get_current_user), db=Depends(get_db)):
    """聚合用户通知。"""
    if NODE_ROLE == "master":
        with _share_action_lock:
            _reconcile_remote_shares(db, user.id)
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
        if user.id in pending_share_approver_ids(payload):
            owner = db.query(UserModel).filter(UserModel.id == c.user_id).first()
            owner_name = owner.username if owner else "未知用户"
            items.append(
                NotificationItem(
                    id=f"share-approval-{c.id}",
                    type="share_approval_request",
                    title="GPU 合并共用申请待处理" if c.target_container_id else "GPU 共用申请待处理",
                    message=f"{owner_name} 申请将 GPU {c.gpu_ids} 合并到现有容器，请同意或拒绝。" if c.target_container_id else f"{owner_name} 申请共用 GPU {c.gpu_ids}，请同意或拒绝。",
                    created_at=c.created_at or now,
                    container_id=c.id,
                    container_name=c.name,
                    gpu_ids=c.gpu_ids or "",
                )
            )

    if NODE_ROLE == "worker":
        for row in db.query(ShareRequestModel).filter(ShareRequestModel.state == "pending").all():
            if user.id not in remote_share_approver_ids(db, row):
                continue
            payload = json.loads(row.payload)
            items.append(NotificationItem(
                id=f"remote-share-approval-{row.id}", type="remote_share_approval_request",
                title="远端 GPU 共用申请待处理",
                message=f"{payload['applicant']} 申请共用 GPU {','.join(map(str, payload['gpu_ids']))}，请同意或拒绝。",
                created_at=row.created_at, container_name=payload["name"],
                gpu_ids=",".join(map(str, payload["gpu_ids"])),
            ))

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
        if lease_reminder_active(c, now):
            remaining = c.expires_at - now
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
                title="GPU 合并申请审批中" if c.target_container_id else "GPU 共用申请审批中",
                message=f"GPU 合并申请正在等待审批（{approved_cnt}/{total_cnt} 已同意）。" if c.target_container_id else f"容器申请 {c.name} 正在等待审批（{approved_cnt}/{total_cnt} 已同意）。",
                created_at=c.created_at or now,
                container_id=c.id,
                container_name=c.name,
                gpu_ids=c.gpu_ids or "",
            )
        )

    events = db.query(UserNotificationModel).filter(UserNotificationModel.user_id == user.id).all()
    items.extend(NotificationItem(
        id=f"event-{event.id}", type=event.type, title=event.title, message=event.message,
        created_at=event.created_at, container_id=event.container_id,
        container_name=event.container_name, read_at=event.read_at,
    ) for event in events)
    items.sort(key=lambda x: x.created_at, reverse=True)
    return NotificationListResponse(unread_count=sum(item.read_at is None for item in items), items=items)


@router.post("/{container_id}/approve-share")
@_synchronized_share_action
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

    will_complete = all(bool(a.get("approved")) or a is me for a in approvers)
    if will_complete:
        applicant = db.query(UserModel).filter(UserModel.id == c.user_id).first()
        if not applicant:
            raise HTTPException(status_code=500, detail="用户不存在")
        quota = check_user_can_provision(db, applicant)
        if not quota.allowed:
            detail = "暂时无法确认申请人的工作区容量，请稍后重试" if not quota.scan_complete else "申请人的工作区已超过磁盘配额，清理文件后才能创建容器"
            raise HTTPException(status_code=400, detail=detail)

    previous_approval = c.pending_share_json
    me["approved"] = True
    me["approved_at"] = datetime.now().isoformat()

    c.pending_share_json = json.dumps(payload, ensure_ascii=False)

    if all(bool(a.get("approved")) for a in approvers):
        lease_days = int(payload.get("lease_days", DEFAULT_LEASE_DAYS))
        try:
            _provision_running_container(db, c, lease_days, previous_approval)
        except HTTPException as exc:
            db.rollback()
            if c.target_container_id and exc.status_code == 400:
                db.refresh(c)
                c.status = "share_rejected"
                c.pending_share_json = None
                c.stopped_at = datetime.now()
                c.name = f"{c.name}-invalid-{c.id}"
                db.commit()
                raise HTTPException(status_code=409, detail="申请条件已变化，原申请已取消，请重新申请") from exc
            raise
        return {"message": "已全部同意，GPU 已合并" if c.target_container_id else "已全部同意，容器已创建"}

    db.commit()
    return {"message": "已记录您的同意"}


@router.post("/{container_id}/reject-share")
@_synchronized_share_action
def reject_share(container_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    c = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
    if not c or c.status != "pending_share_approval":
        raise HTTPException(status_code=404, detail="申请不存在或已处理")
    payload = _parse_share_payload(c.pending_share_json)
    if not payload:
        raise HTTPException(status_code=400, detail="无效的审批数据")
    approvers = payload.get("approvers") or []
    if not any("user_id" in a and int(a["user_id"]) == user.id for a in approvers):
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
    if NODE_ROLE == "master":
        with _share_action_lock:
            _reconcile_remote_shares(db, user.id)
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
@_synchronized_share_action
def delete_container(container_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    from app.container_lifecycle import remove_container_record

    c = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="容器不存在")

    if c.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="无权操作此容器")

    if c.status == "merging" or (c.status == "running" and c.pending_share_json and "old_id" in (_parse_share_payload(c.pending_share_json) or {})):
        raise HTTPException(status_code=409, detail="容器正在合并或等待清理，请稍后重试")

    if c.status in ("provisioning", "share_uncertain"):
        raise HTTPException(status_code=409, detail="Worker 创建结果待人工核查，不能取消")

    if c.status == "pending_share_approval":
        payload = _parse_share_payload(c.pending_share_json)
        if payload and payload.get("request_id"):
            node = get_node(db, c.node_id)
            if not node:
                raise HTTPException(status_code=503, detail="Worker 节点不可用")
            try:
                result = RemoteAgentClient(node.base_url, node.agent_token).cancel_share(payload["request_id"])
            except RemoteAgentError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if result.get("state") != "cancelled" or result.get("request_id") != payload["request_id"]:
                raise HTTPException(status_code=409, detail="Worker 申请未确认取消")
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
