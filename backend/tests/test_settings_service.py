import pytest

from app.settings_service import SettingsValues, parse_bool, validate_settings


def valid_settings(**overrides):
    values = {
        "cpu_mem_gb": 8,
        "gpu_mem_gb_per_gpu": 32,
        "max_gpu_sharing_users": 4,
        "idle_gpu_reclaim_enabled": True,
        "idle_gpu_util_threshold_percent": 5,
        "idle_gpu_memory_threshold_percent": 5,
        "idle_gpu_duration_hours": 24,
    }
    values.update(overrides)
    return SettingsValues(**values)


def test_parse_bool_accepts_common_values_and_safe_default():
    assert parse_bool("true", False)
    assert parse_bool("YES", False)
    assert not parse_bool("off", True)
    assert not parse_bool("unexpected", True)
    assert parse_bool(None, True)


@pytest.mark.parametrize(
    "override",
    [
        {"idle_gpu_util_threshold_percent": -1},
        {"idle_gpu_util_threshold_percent": 101},
        {"idle_gpu_memory_threshold_percent": -1},
        {"idle_gpu_memory_threshold_percent": 101},
        {"idle_gpu_duration_hours": 0},
        {"idle_gpu_duration_hours": 8761},
    ],
)
def test_validate_idle_settings_bounds(override):
    with pytest.raises(ValueError):
        validate_settings(valid_settings(**override))


def test_validate_idle_settings_accepts_boundaries():
    validate_settings(valid_settings(idle_gpu_util_threshold_percent=0, idle_gpu_memory_threshold_percent=100, idle_gpu_duration_hours=8760))
