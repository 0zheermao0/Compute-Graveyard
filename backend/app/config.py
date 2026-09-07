"""应用配置"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
USER_DATA_BASE = Path(os.getenv("USER_DATA_BASE", "/data/users"))
PUBLIC_DATASETS = Path(os.getenv("PUBLIC_DATASETS", "/data/public_datasets"))
SSH_PORT_START = int(os.getenv("SSH_PORT_START", "20000"))
SSH_PORT_END = int(os.getenv("SSH_PORT_END", "21000"))
# 常用服务端口（容器内固定）：Jupyter=8888, TensorBoard=6006, Web=8080
CONTAINER_SERVICE_PORTS = [8888, 6006, 8080]
SERVICE_PORT_START = int(os.getenv("SERVICE_PORT_START", "30000"))
SERVICE_PORT_END = int(os.getenv("SERVICE_PORT_END", "40000"))
DOCKER_BASE_IMAGE = os.getenv("DOCKER_BASE_IMAGE", "nvidia/cuda:12.0-runtime-ubuntu22.04")
DEFAULT_LEASE_DAYS = int(os.getenv("DEFAULT_LEASE_DAYS", "3"))
MAX_LEASE_DAYS = int(os.getenv("MAX_LEASE_DAYS", "7"))
MAX_GPUS_PER_USER = int(os.getenv("MAX_GPUS_PER_USER", "2"))
MAX_CONTAINERS_PER_USER = int(os.getenv("MAX_CONTAINERS_PER_USER", "4"))
# 内存配额默认值（单位 GB），可在管理后台动态覆盖
DEFAULT_CPU_MEM_GB = int(os.getenv("DEFAULT_CPU_MEM_GB", "8"))
DEFAULT_GPU_MEM_GB_PER_GPU = int(os.getenv("DEFAULT_GPU_MEM_GB_PER_GPU", "32"))
# 单卡最多允许多少名不同用户共用（管理员可在后台修改）
DEFAULT_MAX_GPU_SHARING_USERS = int(os.getenv("DEFAULT_MAX_GPU_SHARING_USERS", "4"))
DEFAULT_IDLE_GPU_RECLAIM_ENABLED = os.getenv("IDLE_GPU_RECLAIM_ENABLED", "true")
DEFAULT_IDLE_GPU_UTIL_THRESHOLD_PERCENT = int(os.getenv("IDLE_GPU_UTIL_THRESHOLD_PERCENT", "5"))
DEFAULT_IDLE_GPU_MEMORY_THRESHOLD_PERCENT = int(os.getenv("IDLE_GPU_MEMORY_THRESHOLD_PERCENT", "5"))
DEFAULT_IDLE_GPU_DURATION_HOURS = int(os.getenv("IDLE_GPU_DURATION_HOURS", "24"))
DEFAULT_DISK_QUOTA_GB = float(os.getenv("DEFAULT_DISK_QUOTA_GB", "100"))
if DEFAULT_DISK_QUOTA_GB <= 0:
    raise ValueError("DEFAULT_DISK_QUOTA_GB must be positive")
DEFAULT_DISK_QUOTA_BYTES = max(1, int(DEFAULT_DISK_QUOTA_GB * 1024**3))
DISK_QUOTA_SCAN_INTERVAL_MINUTES = int(os.getenv("DISK_QUOTA_SCAN_INTERVAL_MINUTES", "5"))
DISK_QUOTA_GRACE_HOURS = int(os.getenv("DISK_QUOTA_GRACE_HOURS", "24"))
if DISK_QUOTA_SCAN_INTERVAL_MINUTES <= 0:
    raise ValueError("DISK_QUOTA_SCAN_INTERVAL_MINUTES must be positive")
if DISK_QUOTA_GRACE_HOURS <= 0:
    raise ValueError("DISK_QUOTA_GRACE_HOURS must be positive")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-in-production")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'lab_gpu.db'}")
NOTIFY_WEBHOOK = os.getenv("NOTIFY_WEBHOOK", "")  # 钉钉/飞书 Webhook
