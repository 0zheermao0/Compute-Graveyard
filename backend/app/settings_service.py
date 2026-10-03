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
    MAX_LEASE_DAYS,
    MAX_REPUTATION_SCORE,
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
    idle_gpu_dual_low_enabled: bool = True
    idle_gpu_memory_unchanged_enabled: bool = True
    idle_gpu_shrink_enabled: bool = True
    reputation_initial_score: int = 0
    reputation_idle_warning_points: int = 1
    reputation_idle_reclaim_points: int = 2
    reputation_idle_shrink_points: int = 2
    reputation_expiry_reward_points: int = 2
    reputation_tier1_threshold: int = 5
    reputation_tier1_max_days: int = 5
    reputation_tier2_threshold: int = 10
    reputation_tier2_max_days: int = 3


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
    for key, value in asdict(values).items():
        if key.startswith("reputation_") and (type(value) is not int or not 0 <= value <= MAX_REPUTATION_SCORE):
            raise ValueError(f"{key} 必须为 0~{MAX_REPUTATION_SCORE} 的整数")
    if values.reputation_tier2_threshold <= values.reputation_tier1_threshold:
        raise ValueError("信誉阈值必须递增")
    if not 1 <= values.reputation_tier2_max_days <= values.reputation_tier1_max_days <= MAX_LEASE_DAYS:
        raise ValueError(f"租期必须在 1~{MAX_LEASE_DAYS} 天且第二档不超过第一档")
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
        idle_gpu_dual_low_enabled=parse_bool(rows.get("idle_gpu_dual_low_enabled"), True),
        idle_gpu_memory_unchanged_enabled=parse_bool(rows.get("idle_gpu_memory_unchanged_enabled"), True),
        idle_gpu_shrink_enabled=parse_bool(rows.get("idle_gpu_shrink_enabled"), True),
        **{key: int(rows.get(key, field.default)) for key, field in SettingsValues.__dataclass_fields__.items()
           if key.startswith("reputation_")},
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
        "algorithm": "window-v2",
        "enabled": values.idle_gpu_reclaim_enabled,
        "util": values.idle_gpu_util_threshold_percent,
        "memory": values.idle_gpu_memory_threshold_percent,
        "hours": values.idle_gpu_duration_hours,
        "dual_low_enabled": values.idle_gpu_dual_low_enabled,
        "memory_unchanged_enabled": values.idle_gpu_memory_unchanged_enabled,
        "shrink_enabled": values.idle_gpu_shrink_enabled,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
