import ipaddress
import re
from datetime import datetime
from urllib.parse import urlsplit

from app.config import NODE_ID, NODE_NAME, NODE_PUBLIC_HOST, NODE_ROLE, NODE_SERVICE_SCHEME
from app.database_models import ComputeNodeModel, ContainerModel, UserModel
from app.docker_service import allocate_ssh_port, create_container, get_gpu_info, get_system_load, list_managed_containers, remove_container, stop_container
from app.remote_agent import RemoteAgentClient, RemoteAgentError


_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def normalize_public_host(value: str, required: bool = False) -> str:
    raw = value.strip()
    if not raw:
        if required:
            raise ValueError("节点必须提供公开访问主机名或 IP")
        return ""
    if "://" in raw or any(char in raw for char in "/?#@"):
        raise ValueError("公开访问地址只能填写主机名或 IP")
    unwrapped = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
    try:
        address = ipaddress.ip_address(unwrapped)
    except ValueError:
        address = None
    if address:
        return f"[{address.compressed}]" if address.version == 6 else address.compressed
    try:
        parsed = urlsplit(f"//{raw}")
        if parsed.port is not None:
            raise ValueError("公开访问地址不能包含端口")
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("公开访问地址格式无效") from exc
    if not hostname or not _HOSTNAME_RE.fullmatch(hostname):
        raise ValueError("公开访问地址格式无效")
    return hostname


def build_service_url(scheme: str, host: str, port: int) -> str:
    normalized_scheme = scheme if scheme in {"http", "https"} else "http"
    return f"{normalized_scheme}://{normalize_public_host(host, required=True)}:{int(port)}"


def active_owners(db, node_id: str) -> list[dict]:
    rows = db.query(ContainerModel, UserModel).join(UserModel, ContainerModel.user_id == UserModel.id).filter(
        ContainerModel.node_id == node_id,
        ContainerModel.status == "running",
        ContainerModel.container_id.isnot(None),
    ).all()
    return [
        {
            "container_id": container.container_id,
            "container_name": container.name,
            "gpu_ids": container.gpu_ids or "",
            "ssh_port": container.ssh_port,
            "created_at": container.created_at.isoformat() if container.created_at else None,
            "expires_at": container.expires_at.isoformat(),
            "username": user.username,
            "display_name": user.display_name or user.username,
            "real_name": user.real_name or None,
            "contact_type": user.contact_type or None,
            "contact_value": user.contact_value or None,
        }
        for container, user in rows
    ]


def verified_owners(db, node_id: str, runtime: list[dict]) -> list[dict]:
    running = {row.get("container_id"): row for row in runtime if row.get("container_id") and row.get("status") == "running" and not str(row.get("name") or "").endswith("-merge-old")}
    return [
        {**owner, "gpu_ids": running[owner["container_id"]].get("gpu_ids") or ""}
        for owner in active_owners(db, node_id)
        if owner["container_id"] in running
        and running[owner["container_id"]].get("name") == owner["container_name"]
        and running[owner["container_id"]].get("username") == owner["username"]
    ]


def local_inventory(db) -> dict:
    managed = list_managed_containers()
    gpus = get_gpu_info()
    containers = managed
    return {
        "node_id": NODE_ID,
        "node_name": NODE_NAME,
        "public_host": NODE_PUBLIC_HOST,
        "service_scheme": NODE_SERVICE_SCHEME,
        "role": NODE_ROLE,
        "gpus": gpus,
        "system_load": get_system_load(),
        "containers": containers,
        "owners": verified_owners(db, NODE_ID, managed),
    }


def node_response(node: ComputeNodeModel) -> dict:
    return {
        "id": node.id,
        "name": node.name,
        "base_url": node.base_url,
        "public_host": node.public_host,
        "enabled": bool(node.enabled),
        "schedulable": bool(node.schedulable),
        "last_seen_at": node.last_seen_at,
        "created_at": node.created_at,
        "updated_at": node.updated_at,
        "has_agent_token": bool(node.agent_token),
        "is_local": node.id == NODE_ID,
    }


def get_node(db, node_id: str) -> ComputeNodeModel | None:
    return db.query(ComputeNodeModel).filter(ComputeNodeModel.id == node_id).first()


def inventory_for_node(db, node: ComputeNodeModel) -> dict:
    if node.id == NODE_ID:
        inventory = local_inventory(db)
    else:
        inventory = RemoteAgentClient(node.base_url, node.agent_token).inventory()
    if not isinstance(inventory, dict) or inventory.get("node_id") != node.id:
        raise RemoteAgentError("节点 Agent 身份与配置的节点 ID 不一致")
    if not isinstance(inventory.get("gpus"), list) or not isinstance(inventory.get("containers"), list) or not isinstance(inventory.get("system_load"), dict):
        raise RemoteAgentError("节点 Agent inventory 响应缺少必要字段")
    node.last_seen_at = datetime.now()
    db.commit()
    return inventory


def aggregate_inventories(db, enabled_only: bool = True, local_only: bool = False) -> list[dict]:
    query = db.query(ComputeNodeModel)
    if enabled_only:
        query = query.filter(ComputeNodeModel.enabled.is_(True))
    result = []
    for node in query.order_by(ComputeNodeModel.id).all():
        if local_only and node.id != NODE_ID:
            result.append({"node": node_response(node), "online": False, "inventory": None, "error": None})
            continue
        try:
            inventory = inventory_for_node(db, node)
            result.append({"node": node_response(node), "online": True, "inventory": inventory, "error": None})
        except (RemoteAgentError, RuntimeError, ValueError) as exc:
            result.append({"node": node_response(node), "online": False, "inventory": None, "error": str(exc)})
    return result


def _users_per_gpu(db, node_id: str, inventory: dict | None = None) -> dict[int, set[int]]:
    result: dict[int, set[int]] = {}
    if inventory is None:
        rows = db.query(ContainerModel).filter(ContainerModel.node_id == node_id, ContainerModel.status.in_(["running", "merging"])).all()
        runtime = {row.container_id: {"gpu_ids": row.gpu_ids} for row in rows}
    else:
        runtime = {row.get("container_id"): row for row in inventory.get("containers", []) if row.get("container_id")}
        known = _known_runtime_ids(db, node_id, inventory)
        if not known:
            return result
        rows = db.query(ContainerModel).filter(ContainerModel.node_id == node_id, ContainerModel.container_id.in_(known)).all()
    for row in rows:
        for value in str(runtime[row.container_id].get("gpu_ids") or "").split(","):
            if value.strip():
                result.setdefault(int(value), set()).add(row.user_id)
    return result


def _known_runtime_ids(db, node_id: str, inventory: dict) -> set[str]:
    runtime = {row.get("container_id"): row for row in inventory.get("containers", []) if row.get("container_id")}
    if not runtime:
        return set()
    rows = db.query(ContainerModel, UserModel).join(UserModel, ContainerModel.user_id == UserModel.id).filter(
        ContainerModel.node_id == node_id,
        ContainerModel.status.in_(["running", "merging"]),
        ContainerModel.container_id.in_(runtime),
    ).all()
    return {container.container_id for container, user in rows if runtime[container.container_id].get("status") == "running"
            and not str(runtime[container.container_id].get("name") or "").endswith("-merge-old")
            and runtime[container.container_id].get("name") == container.name
            and runtime[container.container_id].get("username") == user.username}


def _inventory_occupied_gpu_ids(inventory: dict, known_container_ids: set[str] | None = None) -> set[int]:
    known_container_ids = known_container_ids or set()
    return {
        int(value)
        for row in inventory.get("containers", [])
        if row.get("container_id") not in known_container_ids or str(row.get("name") or "").endswith("-merge-old")
        if row.get("status") in {"running", "merging"} or str(row.get("name") or "").endswith("-merge-old")
        for value in str(row.get("gpu_ids") or "").split(",")
        if value.strip()
    }


def _has_capacity(gpu_ids: list[int], users_per_gpu: dict[int, set[int]], applicant_id: int | None, max_share: int | None) -> bool:
    if applicant_id is None or max_share is None:
        return True
    for gpu_id in gpu_ids:
        users = users_per_gpu.get(gpu_id, set())
        if applicant_id not in users and len(users) >= max_share:
            return False
        if applicant_id in users and len(users) > max_share:
            return False
    return True


def worker_share_snapshot(db, node_id: str, inventory: dict, gpu_ids: list[int]) -> list[dict]:
    want = set(gpu_ids)
    owners = inventory.get("owners")
    if not isinstance(owners, list):
        raise ValueError("Worker 未提供可验证的占用者信息")
    master_ids = {row.container_id for row in db.query(ContainerModel).filter(
        ContainerModel.node_id == node_id, ContainerModel.status.in_(["running", "merging"])).all()}
    if any(want & {int(part) for part in str(row.gpu_ids or "").split(",") if part}
           for row in db.query(ContainerModel).filter(ContainerModel.node_id == node_id,
           ContainerModel.status.in_(["running", "merging"])).all()):
        raise ValueError("所选 GPU 混有 Master 占用")
    snapshot = []
    seen = set()
    for item in inventory["containers"]:
        try:
            occupied = {int(part) for part in str(item.get("gpu_ids") or "").split(",") if part}
        except (TypeError, ValueError) as exc:
            raise ValueError("Worker GPU 占用信息无效") from exc
        if not occupied & want or item.get("status") not in ("running", "merging") and not str(item.get("name") or "").endswith("-merge-old"):
            continue
        cid = item.get("container_id")
        matching = [owner for owner in owners if isinstance(owner, dict) and owner.get("container_id") == cid]
        if (not cid or cid in seen or cid in master_ids or len(matching) != 1 or item.get("status") != "running"
                or str(item.get("name") or "").endswith("-merge-old")):
            raise ValueError("所选 GPU 存在未知或 Master 占用")
        owner = matching[0]
        if (owner.get("container_name") != item.get("name") or owner.get("username") != item.get("username")
                or owner.get("gpu_ids") != item.get("gpu_ids")):
            raise ValueError("Worker 占用者信息不一致")
        try:
            if datetime.fromisoformat(owner["expires_at"]) <= datetime.now():
                raise ValueError("Worker 占用者已过期")
        except (KeyError, TypeError) as exc:
            raise ValueError("Worker 占用者有效期无效") from exc
        seen.add(cid)
        snapshot.append({"container_id": cid, "name": item["name"], "username": item["username"], "gpu_ids": sorted(occupied)})
    return sorted(snapshot, key=lambda row: row["container_id"])


def select_node(
    db,
    placement_mode: str,
    requested_node_id: str | None,
    gpu_ids: list[int],
    cpu_only: bool,
    applicant_id: int | None = None,
    max_share: int | None = None,
    allow_worker_share: bool = False,
) -> tuple[ComputeNodeModel, dict]:
    if placement_mode not in {"local", "specific", "auto"}:
        raise ValueError("placement_mode 必须是 local、specific 或 auto")
    if placement_mode == "local":
        requested_node_id = NODE_ID
    if placement_mode in {"local", "specific"}:
        if not requested_node_id:
            raise ValueError("指定节点时必须提供 node_id")
        node = get_node(db, requested_node_id)
        if not node or not node.enabled or not node.schedulable:
            raise ValueError("指定节点不存在、已禁用或不可调度")
        inventory = inventory_for_node(db, node)
        if not cpu_only:
            available_ids = {int(row["index"]) for row in inventory.get("gpus", [])}
            if not set(gpu_ids).issubset(available_ids):
                raise ValueError("指定节点不包含所选 GPU")
            known = _known_runtime_ids(db, node.id, inventory)
            worker_share = allow_worker_share and node.id != NODE_ID and bool(set(gpu_ids) & _inventory_occupied_gpu_ids(inventory, known))
            if worker_share:
                worker_share_snapshot(db, node.id, inventory, gpu_ids)
            elif set(gpu_ids) & _inventory_occupied_gpu_ids(inventory, known):
                raise ValueError("指定节点的所选 GPU 存在未知占用")
            if not worker_share and applicant_id is not None and max_share is not None and not _has_capacity(gpu_ids, _users_per_gpu(db, node.id, inventory), applicant_id, max_share):
                raise ValueError("所选 GPU 已达到共用人数上限")
        return node, inventory

    candidates = []
    nodes = db.query(ComputeNodeModel).filter(
        ComputeNodeModel.enabled.is_(True),
        ComputeNodeModel.schedulable.is_(True),
    ).all()
    if requested_node_id:
        nodes = [node for node in nodes if node.id == requested_node_id]
    for node in nodes:
        try:
            inventory = inventory_for_node(db, node)
        except (RemoteAgentError, RuntimeError, ValueError):
            continue
        running = [row for row in inventory.get("containers", []) if row.get("status") == "running"]
        if cpu_only:
            load = inventory.get("system_load") or {}
            if float(load.get("memory_percent", 100)) >= 95:
                continue
            score = (float(load.get("memory_percent", 0)), float(load.get("cpu_percent", 0)), len(running))
        else:
            available_ids = {int(row["index"]) for row in inventory.get("gpus", [])}
            if not set(gpu_ids).issubset(available_ids):
                continue
            known = _known_runtime_ids(db, node.id, inventory)
            if node.id != NODE_ID and set(gpu_ids) & _inventory_occupied_gpu_ids(inventory):
                continue
            if node.id == NODE_ID and set(gpu_ids) & _inventory_occupied_gpu_ids(inventory, known if applicant_id is not None and max_share is not None else None):
                continue
            users_per_gpu = _users_per_gpu(db, node.id, inventory) if applicant_id is not None and max_share is not None else {}
            if not _has_capacity(gpu_ids, users_per_gpu, applicant_id, max_share):
                continue
            gpu_by_id = {int(row["index"]): row for row in inventory.get("gpus", [])}
            share_score = sum(len(users_per_gpu.get(gpu_id, set())) for gpu_id in gpu_ids)
            score = (share_score, sum(float(gpu_by_id[gpu_id].get("memory_percent") or 0) for gpu_id in gpu_ids), len(running))
        candidates.append((score, node, inventory))
    if not candidates:
        raise ValueError("没有在线且资源满足要求的可调度节点")
    candidates.sort(key=lambda value: (value[0], value[1].id))
    return candidates[0][1], candidates[0][2]


def provision_on_node(node: ComputeNodeModel, name: str, username: str, gpu_ids: list[int], mem_limit_gb: int) -> dict:
    if node.id == NODE_ID:
        ssh_port = allocate_ssh_port()
        if not ssh_port:
            raise RuntimeError("暂无可用 SSH 端口")
        container_id, ssh_password, extra_ports = create_container(name, username, gpu_ids, ssh_port, mem_limit_gb)
        result = {
            "container_id": container_id,
            "ssh_password": ssh_password,
            "ssh_port": ssh_port,
            "extra_ports": extra_ports,
            "access_host": node.public_host or NODE_PUBLIC_HOST,
            "service_scheme": NODE_SERVICE_SCHEME,
        }
    else:
        result = RemoteAgentClient(node.base_url, node.agent_token).create_container(
            {"name": name, "username": username, "gpu_ids": gpu_ids, "mem_limit_gb": mem_limit_gb}
        )
        result["access_host"] = node.public_host or result.get("public_host")
    if not result.get("container_id") or not result.get("ssh_password") or int(result.get("ssh_port") or 0) <= 0:
        raise RemoteAgentError("节点未返回完整的容器访问凭据")
    result["access_host"] = normalize_public_host(str(result.get("access_host") or ""), required=True)
    result["service_scheme"] = result.get("service_scheme") if result.get("service_scheme") in {"http", "https"} else "http"
    return result


def delete_provisioned_container(node: ComputeNodeModel, container_id: str) -> bool:
    if node.id == NODE_ID:
        return remove_container(container_id)
    try:
        RemoteAgentClient(node.base_url, node.agent_token).delete_container(container_id)
        return True
    except RemoteAgentError:
        return False


def stop_on_node(db, container: ContainerModel) -> bool:
    if not container.container_id:
        return True
    if not container.node_id or container.node_id == NODE_ID:
        return stop_container(container.container_id)
    node = get_node(db, container.node_id)
    if not node:
        return False
    try:
        RemoteAgentClient(node.base_url, node.agent_token).stop_container(container.container_id)
        return True
    except RemoteAgentError:
        return False


def delete_on_node(db, container: ContainerModel) -> bool:
    if not container.container_id:
        return True
    if not container.node_id or container.node_id == NODE_ID:
        return remove_container(container.container_id)
    node = get_node(db, container.node_id)
    if not node:
        return False
    try:
        RemoteAgentClient(node.base_url, node.agent_token).delete_container(container.container_id)
        return True
    except RemoteAgentError:
        return False
