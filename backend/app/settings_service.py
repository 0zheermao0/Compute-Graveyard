import hashlib
import json
import logging
from dataclasses import asdict, dataclass

from app.config import (
    DEFAULT_CPU_MEM_GB,
    DEFAULT_GPU_MEM_GB_PER_GPU,
    DEFAULT_IDLE_GPU_DURATION_HOURS,
    DEFAULT_IDLE_GPU_MEMORY_THRESHOLD_PERCENT,
    DEFAULT_IDLE_GPU_RECLAIM_ENABLED,
    DEFAULT_IDLE_GPU_UTIL_THRESHOLD_PERCENT,
    DEFAULT_MAX_GPU_SHARING_USERS,
)
from app.database_models import SystemSettings


@dataclass(frozen=True)
class SettingsValues:
    cpu_mem_gb: int
    gpu_mem_gb_per_gpu: int
    max_gpu_sharing_users: int
    idle_gpu_reclaim_enabled: bool
    idle_gpu_util_threshold_percent: int
    idle_gpu_memory_threshold_percent: int
    idle_gpu_duration_hours: int


def parse_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logging.getLogger(__name__).error("无效布尔配置值 %r，自动回收将关闭", value)
    return False


def validate_settings(values: SettingsValues) -> None:
    if values.cpu_mem_gb < 1:
        raise ValueError("CPU 内存配额最小 1 GB")
    if values.gpu_mem_gb_per_gpu < 1:
        raise ValueError("单卡 GPU 内存配额最小 1 GB")
    if values.max_gpu_sharing_users < 1:
        raise ValueError("单卡最多共用人数至少为 1")
    if not 0 <= values.idle_gpu_util_threshold_percent <= 100:
        raise ValueError("GPU 利用率阈值必须在 0 到 100 之间")
    if not 0 <= values.idle_gpu_memory_threshold_percent <= 100:
        raise ValueError("GPU 显存占用阈值必须在 0 到 100 之间")
    if not 1 <= values.idle_gpu_duration_hours <= 8760:
        raise ValueError("GPU 低利用连续时长必须在 1 到 8760 小时之间")


def load_settings(db) -> SettingsValues:
    rows = {row.key: row.value for row in db.query(SystemSettings).all()}
    values = SettingsValues(
        cpu_mem_gb=int(rows.get("cpu_mem_gb", DEFAULT_CPU_MEM_GB)),
        gpu_mem_gb_per_gpu=int(rows.get("gpu_mem_gb_per_gpu", DEFAULT_GPU_MEM_GB_PER_GPU)),
        max_gpu_sharing_users=int(rows.get("max_gpu_sharing_users", DEFAULT_MAX_GPU_SHARING_USERS)),
        idle_gpu_reclaim_enabled=parse_bool(
            rows.get("idle_gpu_reclaim_enabled") if "idle_gpu_reclaim_enabled" in rows else None,
            parse_bool(DEFAULT_IDLE_GPU_RECLAIM_ENABLED, False),
        ),
        idle_gpu_util_threshold_percent=int(rows.get("idle_gpu_util_threshold_percent", DEFAULT_IDLE_GPU_UTIL_THRESHOLD_PERCENT)),
        idle_gpu_memory_threshold_percent=int(rows.get("idle_gpu_memory_threshold_percent", DEFAULT_IDLE_GPU_MEMORY_THRESHOLD_PERCENT)),
        idle_gpu_duration_hours=int(rows.get("idle_gpu_duration_hours", DEFAULT_IDLE_GPU_DURATION_HOURS)),
    )
    validate_settings(values)
    return values


def save_settings(db, values: SettingsValues) -> SettingsValues:
    validate_settings(values)
    serialized = asdict(values)
    for key, value in serialized.items():
        stored = "true" if value is True else "false" if value is False else str(value)
        row = db.query(SystemSettings).filter(SystemSettings.key == key).first()
        if row:
            row.value = stored
        else:
            db.add(SystemSettings(key=key, value=stored))
    db.commit()
    return values


def idle_policy_signature(values: SettingsValues) -> str:
    payload = {
        "enabled": values.idle_gpu_reclaim_enabled,
        "util": values.idle_gpu_util_threshold_percent,
        "memory": values.idle_gpu_memory_threshold_percent,
        "hours": values.idle_gpu_duration_hours,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
