"""Tests for ``src.services.realtime_volume_cache``.

Covers sliding-window pruning, slope computation, current-ratio lookup,
minute-bucket deduplication, and thread safety.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from src.services.realtime_volume_cache import (
    RealtimeVolumeCache,
    get_default_cache,
    reset_default_cache,
)


def _ts(minutes_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def test_record_and_current_ratio_round_trip():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 1.5)
    assert cache.current_ratio("600519.SH") == pytest.approx(1.5)
    assert cache.size("600519.SH") == 1


def test_record_ignores_none_and_non_finite():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", None)
    cache.record("600519.SH", float("inf"))
    cache.record("600519.SH", float("nan"))
    assert cache.size() == 0


def test_minute_bucket_dedup_overwrites_previous():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 1.2, at=_ts(2))
    cache.record("600519.SH", 1.4, at=_ts(1))
    cache.record("600519.SH", 1.5, at=_ts(0))
    # Last value wins within the same minute.
    assert cache.size("600519.SH") == 3
    assert cache.current_ratio("600519.SH") == pytest.approx(1.5)


def test_slope_with_two_points():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 1.0, at=_ts(20))
    cache.record("600519.SH", 2.0, at=_ts(10))
    # 10 minutes apart, delta 1.0 → 0.1 per minute.
    slope = cache.slope("600519.SH", window_minutes=30)
    assert slope is not None
    assert slope == pytest.approx(0.1, abs=1e-6)


def test_slope_returns_none_for_single_point():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 1.5)
    assert cache.slope("600519.SH") is None


def test_slope_ignores_points_outside_window():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 0.5, at=_ts(45))  # outside window
    cache.record("600519.SH", 2.0, at=_ts(10))
    cache.record("600519.SH", 2.5, at=_ts(0))
    slope = cache.slope("600519.SH", window_minutes=30)
    # Only the last two points are within the 30-minute window: 2.0 → 2.5 over 10 min
    assert slope is not None
    assert slope == pytest.approx(0.05, abs=1e-6)


def test_prune_drops_expired():
    cache = RealtimeVolumeCache(retention_minutes=10)
    # Both points are already older than the 10-minute retention window so
    # the per-record prune wouldn't drop them on insert — but prune() must.
    cache.record("600519.SH", 1.0, at=_ts(20))
    cache.record("600519.SH", 2.0, at=_ts(15))
    removed = cache.prune()
    assert removed == 2
    assert cache.size("600519.SH") == 0


def test_snapshot_returns_chronological_points():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 1.0, at=_ts(15))
    cache.record("600519.SH", 2.0, at=_ts(5))
    cache.record("600519.SH", 3.0, at=_ts(10))
    points = cache.snapshot("600519.SH", window_minutes=30)
    times = [p[0] for p in points]
    assert times == sorted(times)
    assert len(points) == 3


def test_record_many_is_bulk_safe():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record_many([
        ("600519.SH", 1.5),
        ("000001.SZ", None),
        ("AAPL", 2.3),
    ])
    assert cache.size() == 2


def test_thread_safety_smoke():
    cache = RealtimeVolumeCache(retention_minutes=30)

    def worker(prefix: str):
        for i in range(100):
            cache.record(f"{prefix}{i % 5}", float(i))

    threads = [threading.Thread(target=worker, args=(f"X{i}.SH",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cache.size() == 40  # 5 symbols * 100 records each


def test_default_singleton_lifecycle():
    reset_default_cache()
    cache_a = get_default_cache()
    cache_b = get_default_cache()
    assert cache_a is cache_b
    cache_a.record("600519.SH", 1.5)
    reset_default_cache()
    cache_c = get_default_cache()
    assert cache_c is not cache_a
    assert cache_c.size() == 0