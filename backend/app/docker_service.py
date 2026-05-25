"""
Docker 容器管理服务
通过 socket 与宿主机 Docker 通信
"""
import os
import secrets
import subprocess
import json
from typing import List, Optional, Dict, Any

import docker
from docker.errors import DockerException

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
    except DockerException:
        pass
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
    except DockerException:
        pass
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
    path = os.path.join(USER_DATA_BASE, username)
    os.makedirs(path, exist_ok=True)
    return path


def create_container(
    name: str,
    username: str,
    gpu_ids: List[int],
    ssh_port: int,
    mem_limit_gb: int = 8,
) -> tuple[Optional[str], str, Dict[int, int]]:
    """
    创建容器（GPU 或纯 CPU），随机 SSH 密码，常用端口随机映射。
    返回 (container_id, ssh_password, extra_ports {容器端口: 宿主机端口})
    """
    ensure_user_dir(username)
    user_workspace = os.path.join(USER_DATA_BASE, username)
    ssh_password = secrets.token_urlsafe(12)

    extra_ports_map = allocate_service_ports()
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
            mem_limit=f"{mem_limit_gb}g",
            shm_size="32g",
        )
        cid = container.id if hasattr(container, "id") else str(container) if container else None
        return (cid, ssh_password, extra_ports_map)
    except DockerException as e:
        raise RuntimeError(f"创建容器失败: {e}") from e


def _has_nvidia_runtime() -> bool:
    """检测是否有 nvidia runtime"""
    try:
        client = get_docker_client()
        info = client.info()
        return "nvidia" in str(info.get("Runtimes", {})).lower()
    except Exception:
        return False


def stop_container(container_id: str) -> bool:
    """停止容器"""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        c.stop()
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
    except DockerException:
        return False


def _parse_mib(s: str) -> int:
    """解析 nvidia-smi 的 MiB 数值"""
    s = str(s).replace("MiB", "").replace(" ", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return 0


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
                gpus.append({
                    "index": idx,
                    "name": name,
                    "memory_used_mb": mem_used,
                    "memory_total_mb": mem_total or 1,
                    "memory_percent": round(mem_used / (mem_total or 1) * 100, 1),
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
