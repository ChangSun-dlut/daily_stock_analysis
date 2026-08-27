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


def test_record_1min_kline_feeds_slope_and_peak():
    import pandas as pd

    cache = RealtimeVolumeCache(retention_minutes=30)
    # 构造 5 根 1 分钟 K 线，最后一分钟成交量相对上一根 +2.4 倍日均单分钟量，
    # 应换算成约 2.4 的分钟级当量比。
    avg_daily_volume = 100_000_000  # 1 亿股/日
    minute_expect = avg_daily_volume / 240  # ≈ 416666 股/分钟
    volumes = [minute_expect] * 4 + [minute_expect * (1 + 2.4)]
    bars = pd.DataFrame({"volume": volumes})
    at = datetime.now(timezone.utc)
    cache.record_1min_kline(
        "600519.SH",
        bars=bars,
        avg_daily_volume=avg_daily_volume,
        elapsed_minutes=120,
        at=at,
    )
    assert cache.current_ratio("600519.SH") == pytest.approx(2.4, abs=1e-6)

    # 再喂一根更高脉冲，验证峰值与斜率联动
    bars2 = pd.DataFrame({"volume": [minute_expect] * 4 + [minute_expect * (1 + 4.0)]})
    cache.record_1min_kline(
        "600519.SH",
        bars=bars2,
        avg_daily_volume=avg_daily_volume,
        elapsed_minutes=121,
        at=at + __import__("datetime").timedelta(seconds=60),
    )
    assert cache.peak_ratio("600519.SH", window_minutes=30) == pytest.approx(4.0, abs=1e-6)


def test_record_1min_kline_invalid_inputs_are_noop():
    import pandas as pd

    cache = RealtimeVolumeCache(retention_minutes=30)
    # 少于 2 根 K 线
    assert cache.record_1min_kline("600000.SH", bars=pd.DataFrame({"volume": [1]}),
                                   avg_daily_volume=1e8, elapsed_minutes=120) is None
    # 非交易日（elapsed<=0）
    assert cache.record_1min_kline("600000.SH", bars=pd.DataFrame({"volume": [1, 2]}),
                                   avg_daily_volume=1e8, elapsed_minutes=0) is None
    assert cache.size() == 0


def test_record_1min_kline_uses_real_minute_timestamps():
    """回归：遍历全部 1 分钟 K 线、用每根真实时间戳写入，而非只取最后两根。

    这是修复「盘中主链路 fallback 后 1 分钟预警消失 / 粒度退化」的核心行为：
    即便刷新间隔 > 1 分钟，单次喂入也能补齐缺失的分钟点，缓存里是真正的
    1 分钟粒度序列。
    """
    import pandas as pd
    from datetime import datetime

    cache = RealtimeVolumeCache(retention_minutes=30)
    avg_daily_volume = 100_000_000
    minute_expect = avg_daily_volume / 240

    # 5 根 1 分钟 K 线，time 列给出真实分钟（09:31 ~ 09:35）
    times = [datetime(2026, 8, 27, 9, 31 + i) for i in range(5)]
    volumes = [minute_expect] * 4 + [minute_expect * (1 + 2.4)]  # 09:35 放量
    bars = pd.DataFrame({"volume": volumes, "time": times})
    cache.record_1min_kline(
        "600519.SH",
        bars=bars,
        avg_daily_volume=avg_daily_volume,
        elapsed_minutes=120,
    )

    # 5 根 K 线 -> 4 个相邻增量点（旧的实现只取最后 1 个点），时间戳按真实分钟分布
    snap = cache.snapshot("600519.SH", window_minutes=30)
    assert len(snap) == 4, snap
    first_ts, last_ts = snap[0][0], snap[-1][0]
    # 相邻点间隔约 60 秒（真实分钟），而非被 dedup 合并成一个
    assert abs((last_ts - first_ts).total_seconds() - 180) < 5, (first_ts, last_ts)
    # 最后一根（09:35）相对上一根的增量当量比 = 2.4
    assert snap[-1][1] == pytest.approx(2.4, abs=1e-6)


def test_record_1min_kline_falls_back_to_backdated_timestamps():
    """无 time 列时，用 at 倒推 1 分钟间隔兜底，保证不被 dedup 合并。"""
    import pandas as pd
    from datetime import datetime, timezone

    cache = RealtimeVolumeCache(retention_minutes=30)
    avg_daily_volume = 100_000_000
    minute_expect = avg_daily_volume / 240
    volumes = [minute_expect] * 4 + [minute_expect * (1 + 2.4)]
    bars = pd.DataFrame({"volume": volumes})  # 无 time 列
    at = datetime.now(timezone.utc)
    cache.record_1min_kline(
        "600519.SH",
        bars=bars,
        avg_daily_volume=avg_daily_volume,
        elapsed_minutes=120,
        at=at,
    )
    snap = cache.snapshot("600519.SH", window_minutes=30)
    assert len(snap) == 4, snap
    assert abs((snap[-1][0] - snap[0][0]).total_seconds() - 180) < 5


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