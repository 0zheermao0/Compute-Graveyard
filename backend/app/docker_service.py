"""
Docker 容器管理服务
通过 socket 与宿主机 Docker 通信
"""
import os
import secrets
import subprocess
import json
import hashlib
from pathlib import Path
from typing import List, Optional, Dict, Any

import docker
from docker.errors import DockerException, NotFound

from app.config import (
    USER_DATA_BASE,
    PUBLIC_DATASETS,
    SSH_PORT_START,
    SSH_PORT_END,
    DOCKER_BASE_IMAGE,
    CONTAINER_SERVICE_PORTS,
    SERVICE_PORT_START,
    SERVICE_PORT_END,
)


def get_docker_client():
    """获取 Docker 客户端，使用宿主机 socket"""
    return docker.from_env()


def get_used_ssh_ports() -> set:
    """获取已占用的 SSH 端口"""
    used = set()
    try:
        client = get_docker_client()
        for c in client.containers.list(all=True):
            for port, binds in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
                if port == "22/tcp" and binds:
                    for b in binds:
                        if b.get("HostPort"):
                            used.add(int(b["HostPort"]))
    except DockerException as exc:
        raise RuntimeError("读取 Docker 端口占用失败") from exc
    return used


def allocate_ssh_port() -> Optional[int]:
    """分配一个未占用的 SSH 端口"""
    used = get_used_ssh_ports()
    for p in range(SSH_PORT_START, SSH_PORT_END):
        if p not in used:
            return p
    return None


def get_used_host_ports() -> set:
    """获取所有已占用的宿主机端口"""
    used = set()
    try:
        client = get_docker_client()
        for c in client.containers.list(all=True):
            for port, binds in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
                if binds:
                    for b in binds:
                        if b.get("HostPort"):
                            used.add(int(b["HostPort"]))
    except DockerException as exc:
        raise RuntimeError("读取 Docker 端口占用失败") from exc
    return used


def allocate_service_ports() -> Optional[Dict[int, int]]:
    """为常用服务端口分配随机的宿主机端口。返回 {容器端口: 宿主机端口}"""
    used = get_used_host_ports()
    result = {}
    for container_port in CONTAINER_SERVICE_PORTS:
        found = None
        for p in range(SERVICE_PORT_START, SERVICE_PORT_END):
            if p not in used and p not in result.values():
                found = p
                break
        if found is None:
            return None
        result[container_port] = found
        used.add(found)
    return result


def ensure_user_dir(username: str) -> str:
    """确保用户目录存在"""
    if not username or "/" in username or "\\" in username or username in {".", ".."}:
        raise ValueError("用户名包含非法路径字符")
    base = Path(USER_DATA_BASE)
    if not base.exists() or not base.is_dir():
        raise RuntimeError("用户工作区存储路径不可用")
    path = base / username
    if path.is_symlink():
        raise RuntimeError("用户工作区路径非法")
    path.mkdir(exist_ok=True)
    return str(path)


def create_container(
    name: str,
    username: str,
    gpu_ids: List[int],
    ssh_port: int,
    mem_limit_gb: int = 8,
    workspace_path: Optional[str] = None,
    ssh_password: Optional[str] = None,
    request_id: Optional[str] = None,
    extra_ports_map: Optional[Dict[int, int]] = None,
) -> tuple[Optional[str], Optional[str], Dict[int, int]]:
    """
    创建容器（GPU 或纯 CPU），随机 SSH 密码，常用端口随机映射。
    返回 (container_id, ssh_password, extra_ports {容器端口: 宿主机端口})
    """
    user_workspace = workspace_path if workspace_path is not None else ensure_user_dir(username)
    ssh_password = ssh_password if ssh_password is not None else secrets.token_urlsafe(12)

    extra_ports_map = extra_ports_map if extra_ports_map is not None else allocate_service_ports()
    if not extra_ports_map:
        raise RuntimeError("暂无可用服务端口，请稍后重试")

    ports_map = {"22/tcp": ssh_port}
    for cp, hp in extra_ports_map.items():
        ports_map[f"{cp}/tcp"] = hp

    try:
        client = get_docker_client()
        try:
            client.images.get(DOCKER_BASE_IMAGE)
        except docker.errors.ImageNotFound:
            client.images.pull(DOCKER_BASE_IMAGE)

        volumes = {user_workspace: {"bind": "/workspace", "mode": "rw"}}
        if os.path.exists(str(PUBLIC_DATASETS)):
            volumes[str(PUBLIC_DATASETS)] = {"bind": "/datasets", "mode": "ro"}

        device_requests = None
        if gpu_ids:
            device_requests = [
                docker.types.DeviceRequest(
                    driver="nvidia",
                    device_ids=[str(i) for i in sorted(gpu_ids)],
                    capabilities=[["gpu"]],
                )
            ]

        container = client.containers.run(
            DOCKER_BASE_IMAGE,
            name=name,
            detach=True,
            device_requests=device_requests,
            ports=ports_map,
            volumes=volumes,
            environment={"SSH_PASSWORD": ssh_password, "TZ": "Asia/Shanghai"},
            labels={
                "compute-graveyard.managed": "true",
                "compute-graveyard.username": username,
                "compute-graveyard.gpu_ids": ",".join(map(str, sorted(gpu_ids))),
                **({"compute-graveyard.request_id": request_id} if request_id else {}),
            },
            mem_limit=f"{mem_limit_gb}g",
            shm_size="32g",
        )
        cid = container.id if hasattr(container, "id") else str(container) if container else None
        return (cid, ssh_password, extra_ports_map)
    except DockerException as e:
        raise RuntimeError(f"创建容器失败: {e}") from e


def _merge_names(name: str) -> tuple[str, str]:
    return f"{name[:85]}-merge-old", f"{name[:85]}-merge-new"


def _merge_identity(container, name: str, username: str, old_gpu_ids: list[int]) -> None:
    container.reload()
    labels = container.attrs.get("Config", {}).get("Labels") or {}
    if (container.name != name and container.name != _merge_names(name)[0]) or labels.get("compute-graveyard.managed") != "true" or labels.get("compute-graveyard.username") != username or labels.get("compute-graveyard.gpu_ids") != ",".join(map(str, sorted(old_gpu_ids))):
        raise RuntimeError("目标容器身份不匹配")


def rollback_gpu_merge(container_id: str, name: str, username: str, old_gpu_ids: list[int]) -> None:
    client = get_docker_client()
    backup_name, new_name = _merge_names(name)
    old = client.containers.get(container_id)
    _merge_identity(old, name, username, old_gpu_ids)
    try:
        replacement = client.containers.get(new_name)
    except NotFound:
        replacement = None
    started = False
    was_running = False
    if replacement:
        replacement.reload()
        labels = replacement.attrs.get("Config", {}).get("Labels") or {}
        if labels.get("compute-graveyard.merge_source") != container_id:
            raise RuntimeError("替代容器身份不匹配，无法回滚")
        started = replacement.status != "created"
        was_running = replacement.status == "running"
        if was_running:
            replacement.stop()
            replacement.reload()
            if replacement.status == "running":
                raise RuntimeError("替代容器未能停止，保留两份容器以避免数据丢失")
    try:
        old.reload()
        if old.name == backup_name:
            old.rename(name)
        if old.status != "running":
            old.start()
        old.reload()
        if old.status != "running":
            raise RuntimeError("原容器恢复运行失败")
    except Exception as exc:
        if replacement and was_running:
            try:
                replacement.reload()
                if replacement.status != "running":
                    replacement.start()
                    replacement.reload()
                if replacement.status != "running":
                    raise RuntimeError("替代容器未能恢复运行")
            except Exception as recovery_error:
                raise RuntimeError("原容器与替代容器均未能恢复运行，两份文件层均已保留") from recovery_error
        raise RuntimeError("原容器未能恢复运行，替代容器文件层已保留") from exc
    if replacement and started:
        raise RuntimeError("替代容器可能有新增文件，已保留其文件层等待人工核对")
    if replacement:
        replacement.remove()


def merge_container_gpus(container_id: str, name: str, username: str, old_gpu_ids: list[int], gpu_ids: list[int], ssh_port: int, extra_ports: dict, ssh_password_hash: str, mem_limit_gb: int, workspace_path: Optional[str] = None) -> str:
    client = get_docker_client()
    old = client.containers.get(container_id)
    _merge_identity(old, name, username, old_gpu_ids)
    if old.status != "running" or not ssh_password_hash or not set(old_gpu_ids).issubset(gpu_ids) or len(gpu_ids) <= len(old_gpu_ids):
        raise RuntimeError("目标容器状态或 GPU 参数已变化")
    config = old.attrs.get("Config") or {}
    host = old.attrs.get("HostConfig") or {}
    mounts = old.attrs.get("Mounts") or []
    workspace = workspace_path if workspace_path is not None else os.path.join(USER_DATA_BASE, username)
    if not any(m.get("Destination") == "/workspace" and m.get("Source") == workspace and m.get("RW") for m in mounts):
        raise RuntimeError("目标工作区挂载不匹配")
    if any(m.get("Destination") not in {"/workspace", "/datasets"} or m.get("Type") != "bind" for m in mounts):
        raise RuntimeError("目标容器包含不受支持的挂载")
    if any(m.get("Destination") == "/datasets" and (m.get("Source") != str(PUBLIC_DATASETS) or m.get("RW")) for m in mounts):
        raise RuntimeError("公共数据集挂载不符合只读配置")
    unsupported = ("CapAdd", "CapDrop", "Devices", "SecurityOpt", "Tmpfs", "ExtraHosts", "Dns", "DnsSearch", "DnsOptions", "Ulimits", "GroupAdd", "Sysctls", "Links", "VolumesFrom", "Mounts", "NanoCpus", "CpuQuota", "CpuPeriod", "CpuShares", "CpusetCpus", "ReadonlyRootfs", "AutoRemove", "PublishAllPorts", "OomKillDisable", "CgroupParent", "MemoryReservation", "OomScoreAdj", "Init")
    if host.get("NetworkMode") not in {"default", "bridge"} or host.get("Privileged") or any(host.get(key) for key in unsupported) or host.get("Runtime") not in {None, "", "runc"} or host.get("PidMode") or host.get("IpcMode") not in {None, "private", ""} or host.get("RestartPolicy", {}).get("Name") not in {None, "", "no"}:
        raise RuntimeError("目标容器包含不受支持的运行设置")
    bindings = host.get("PortBindings") or {}
    expected = {"22/tcp": int(ssh_port), **{f"{int(k)}/tcp": int(v) for k, v in extra_ports.items()}}
    actual = {key: int(value[0]["HostPort"]) for key, value in bindings.items() if value}
    if actual != expected:
        raise RuntimeError("目标容器端口与数据库不一致")
    env = config.get("Env") or []
    if not any(hashlib.sha256(value.split("=", 1)[1].encode()).hexdigest() == ssh_password_hash for value in env if value.startswith("SSH_PASSWORD=")):
        raise RuntimeError("目标容器凭据与数据库不一致")
    backup_name, new_name = _merge_names(name)
    try:
        client.containers.get(new_name)
        raise RuntimeError("已有未完成的 GPU 合并，请先恢复")
    except NotFound:
        pass
    image = None
    try:
        old.stop()
        image = old.commit(repository="compute-graveyard-merge", tag=container_id[:32], changes="ENV SSH_PASSWORD=")
        image_env = (image.attrs.get("Config") or {}).get("Env") or []
        if "SSH_PASSWORD=" not in image_env or any(value.startswith("SSH_PASSWORD=") and value != "SSH_PASSWORD=" for value in image_env):
            raise RuntimeError("临时镜像包含明文凭据")
        old.rename(backup_name)
        volumes = {m["Source"]: {"bind": m["Destination"], "mode": "rw" if m["RW"] else "ro"} for m in mounts}
        labels = dict(config.get("Labels") or {})
        labels["compute-graveyard.gpu_ids"] = ",".join(map(str, sorted(gpu_ids)))
        labels["compute-graveyard.merge_source"] = container_id
        replacement = client.containers.create(
            image.id, name=new_name, detach=True,
            device_requests=[docker.types.DeviceRequest(driver="nvidia", device_ids=[str(i) for i in sorted(gpu_ids)], capabilities=[["gpu"]])],
            ports=expected, volumes=volumes, environment=env, labels=labels,
            mem_limit=max(int(host.get("Memory") or 0), mem_limit_gb * 1024 ** 3),
            memswap_limit=max(int(host.get("MemorySwap") or 0), mem_limit_gb * 2 * 1024 ** 3) if int(host.get("MemorySwap") or 0) != -1 else -1,
            shm_size=int(host.get("ShmSize") or 0) or "32g",
            working_dir=config.get("WorkingDir") or None, user=config.get("User") or None,
            command=config.get("Cmd") or None, entrypoint=config.get("Entrypoint") or None,
        )
        replacement.start()
        replacement.reload()
        if replacement.status != "running":
            raise RuntimeError("替代容器未运行")
        return replacement.id
    except Exception as exc:
        try:
            rollback_gpu_merge(container_id, name, username, old_gpu_ids)
        except Exception as recovery_error:
            raise RuntimeError("GPU 合并失败且原容器未能自动恢复，请联系管理员处理") from recovery_error
        if image:
            try:
                client.images.remove(image.id)
            except DockerException:
                pass
        raise RuntimeError("GPU 合并失败，原容器已恢复") from exc


def finalize_gpu_merge(container_id: str, name: str, replacement_id: str, username: str, old_gpu_ids: list[int]) -> None:
    client = get_docker_client()
    try:
        old = client.containers.get(container_id)
    except NotFound:
        old = None
    if old:
        _merge_identity(old, name, username, old_gpu_ids)
    replacement = client.containers.get(replacement_id)
    replacement.reload()
    if replacement.name not in {_merge_names(name)[1], name} or replacement.status != "running" or (replacement.attrs.get("Config", {}).get("Labels") or {}).get("compute-graveyard.merge_source") != container_id:
        raise RuntimeError("替代容器身份或状态不匹配")
    if old:
        old.remove()
    if replacement.name != name:
        replacement.rename(name)
    try:
        client.images.remove(f"compute-graveyard-merge:{container_id[:32]}")
    except DockerException:
        pass


def _has_nvidia_runtime() -> bool:
    """检测是否有 nvidia runtime"""
    try:
        client = get_docker_client()
        info = client.info()
        return "nvidia" in str(info.get("Runtimes", {})).lower()
    except Exception:
        return False


def is_managed_container(container_id: str) -> Optional[bool]:
    try:
        container = get_docker_client().containers.get(container_id)
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        return labels.get("compute-graveyard.managed") == "true"
    except NotFound:
        return None
    except DockerException:
        return False


def stop_container(container_id: str) -> bool:
    """停止容器"""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        c.stop()
        return True
    except NotFound:
        return True
    except DockerException:
        return False


def remove_container(container_id: str) -> bool:
    """删除容器（不删除挂载的宿主机目录）"""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        c.remove(force=True)
        return True
    except NotFound:
        return True
    except DockerException:
        return False


def list_managed_containers(include_request_id: bool = False) -> List[Dict[str, Any]]:
    try:
        client = get_docker_client()
        rows = []
        for container in client.containers.list(all=True, filters={"label": "compute-graveyard.managed=true"}):
            ports = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
            labels = container.attrs.get("Config", {}).get("Labels") or {}
            extra_ports = {}
            ssh_port = 0
            for port, bindings in ports.items():
                if not bindings:
                    continue
                host_port = int(bindings[0]["HostPort"])
                container_port = int(port.split("/", 1)[0])
                if container_port == 22:
                    ssh_port = host_port
                else:
                    extra_ports[str(container_port)] = host_port
            rows.append({
                "container_id": container.id,
                "name": container.name,
                "status": "merging" if container.name.endswith("-merge-old") else container.status,
                "username": labels.get("compute-graveyard.username", ""),
                "gpu_ids": labels.get("compute-graveyard.gpu_ids", ""),
                **({"request_id": labels.get("compute-graveyard.request_id")} if include_request_id else {}),
                "ssh_port": ssh_port,
                "extra_ports": extra_ports,
            })
        return rows
    except DockerException as e:
        raise RuntimeError(f"读取容器列表失败: {e}") from e


def _parse_mib(s: str) -> Optional[int]:
    """解析 nvidia-smi 的 MiB 数值"""
    s = str(s).replace("MiB", "").replace(" ", "").strip()
    try:
        value = int(float(s))
        return value if value >= 0 else None
    except (ValueError, TypeError):
        return None


def get_gpu_info() -> List[Dict[str, Any]]:
    """通过 nvidia-smi 获取 GPU 信息"""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,temperature.gpu,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                idx = int(parts[0]) if parts[0].strip().isdigit() else len(gpus)
                name = parts[1]
                mem_used = _parse_mib(parts[2])
                mem_total = _parse_mib(parts[3])
                try:
                    temp = int(parts[4]) if len(parts) > 4 else None
                except (ValueError, TypeError):
                    temp = None
                try:
                    util = int(str(parts[5]).replace("%", "").strip()) if len(parts) > 5 else None
                except (ValueError, TypeError):
                    util = None
                memory_percent = None
                if mem_used is not None and mem_total is not None and mem_total > 0:
                    memory_percent = round(mem_used / mem_total * 100, 1)
                gpus.append({
                    "index": idx,
                    "name": name,
                    "memory_used_mb": mem_used,
                    "memory_total_mb": mem_total,
                    "memory_percent": memory_percent,
                    "temperature": temp,
                    "utilization": util,
                })
        return gpus
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return []


def get_system_load() -> Dict[str, float]:
    """获取系统负载（CPU、内存、磁盘）"""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(USER_DATA_BASE) if USER_DATA_BASE else psutil.disk_usage("/")
        return {
            "cpu_percent": round(cpu, 1),
            "memory_used_gb": round(mem.used / (1024**3), 2),
            "memory_total_gb": round(mem.total / (1024**3), 2),
            "memory_percent": round(mem.percent, 1),
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "disk_total_gb": round(disk.total / (1024**3), 2),
        }
    except ImportError:
        return {
            "cpu_percent": 0,
            "memory_used_gb": 0,
            "memory_total_gb": 0,
            "memory_percent": 0,
            "disk_free_gb": 0,
            "disk_total_gb": 0,
        }
