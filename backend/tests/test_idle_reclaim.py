from datetime import datetime, timedelta

from app.scheduler import gpu_set_is_low, next_idle_window


def test_all_assigned_gpus_must_be_strictly_low():
    metrics = {
        0: {"utilization": 4, "memory_percent": 4.9},
        1: {"utilization": 4, "memory_percent": 5},
    }
    assert gpu_set_is_low([0], metrics, 5, 5)
    assert not gpu_set_is_low([0, 1], metrics, 5, 5)


def test_missing_or_invalid_gpu_metric_is_not_low():
    assert not gpu_set_is_low([0], {}, 5, 5)
    assert not gpu_set_is_low([0], {0: {"utilization": None, "memory_percent": 0}}, 5, 5)
    assert not gpu_set_is_low([0], {0: {"utilization": 0, "memory_percent": None}}, 5, 5)


def test_idle_window_resets_on_high_sample_and_large_gap():
    start = datetime(2026, 1, 1, 0, 0)
    low_since, last = next_idle_window(None, None, start, True)
    assert low_since == start
    assert last == start

    next_time = start + timedelta(minutes=5)
    low_since, last = next_idle_window(low_since, last, next_time, True)
    assert low_since == start
    assert last == next_time

    gap_time = next_time + timedelta(minutes=16)
    low_since, last = next_idle_window(low_since, last, gap_time, True)
    assert low_since == gap_time
    assert last == gap_time

    high_time = gap_time + timedelta(minutes=5)
    low_since, last = next_idle_window(low_since, last, high_time, False)
    assert low_since is None
    assert last == high_time
