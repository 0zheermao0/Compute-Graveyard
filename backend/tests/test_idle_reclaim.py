from datetime import datetime, timedelta

from app.scheduler import gpu_set_is_low, next_idle_window, next_idle_stages


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


def test_resident_memory_requires_unchanged_previous_sample():
    metrics = {0: {"utilization": 0, "memory_percent": 80, "memory_used_mb": 16000}}
    assert not gpu_set_is_low([0], metrics, 5, 5)
    assert not gpu_set_is_low([0], metrics, 5, 5, {"0": 15999})
    assert gpu_set_is_low([0], metrics, 5, 5, {"0": 16000})
    metrics[0]["utilization"] = 1
    assert not gpu_set_is_low([0], metrics, 5, 5, {"0": 16000})


def test_resident_memory_must_be_positive_and_every_gpu_abnormal():
    metrics = {
        0: {"utilization": 0, "memory_percent": 80, "memory_used_mb": 16000},
        1: {"utilization": 10, "memory_percent": 80, "memory_used_mb": 16000},
    }
    assert not gpu_set_is_low([0, 1], metrics, 5, 5, {"0": 16000, "1": 16000})
    metrics[1]["utilization"] = 2
    metrics[1]["memory_percent"] = 1
    assert gpu_set_is_low([0, 1], metrics, 5, 5, {"0": 16000})
    metrics[0]["memory_used_mb"] = 0
    assert not gpu_set_is_low([0], metrics, 5, 5, {"0": 0})


def test_invalid_percent_is_never_abnormal():
    for bad in (float("nan"), float("inf"), -1, 101, "0", True):
        assert not gpu_set_is_low([0], {0: {"utilization": bad, "memory_percent": 0}}, 5, 5)
        assert not gpu_set_is_low([0], {0: {"utilization": 0, "memory_percent": bad}}, 5, 5)


def test_five_stage_coverage_and_reset():
    start = datetime(2026, 1, 1)
    duration = timedelta(hours=1)
    state = (None, None, 0, None)
    for minutes in range(0, 61, 5):
        now = start + timedelta(minutes=minutes)
        state = next_idle_stages(*state, now, True, duration)
        assert state[0] == start
        if minutes == 50:
            assert state[2] == 31
            state = (*state[:3], now)
    assert state[2] == 31
    assert state[3] == start + timedelta(minutes=50)
    state = next_idle_stages(*state, start + timedelta(minutes=65), False, duration)
    assert state == (None, start + timedelta(minutes=65), 0, None)


def test_unsampled_stage_and_long_gap_restart_cycle():
    start = datetime(2026, 1, 1)
    duration = timedelta(hours=1)
    # A short scheduling interruption skips the entire 10%-30% stage.
    state = (start, start + timedelta(minutes=5), 1, None)
    now = start + timedelta(minutes=19)
    assert next_idle_stages(*state, now, True, duration) == (now, now, 1, None)
    state = (start, start, 1, start)
    now = start + timedelta(minutes=16)
    assert next_idle_stages(*state, now, True, duration) == (now, now, 1, None)
