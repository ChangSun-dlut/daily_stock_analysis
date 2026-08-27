"""Tests for ``volume_spike_rt`` realtime indicator normalization + evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.alert_indicators import (
    DEFAULT_VOLUME_SPIKE_RT_MIN_RATIO,
    DEFAULT_VOLUME_SPIKE_RT_MIN_SLOPE,
    DEFAULT_VOLUME_SPIKE_RT_WINDOW_MINUTES,
    REALTIME_ALERT_TYPES,
    evaluate_realtime_indicator_alert,
    normalize_realtime_indicator_parameters,
    threshold_for_realtime_indicator,
)
from src.services.realtime_volume_cache import RealtimeVolumeCache


# Fixed evaluation clock during A-share morning trading (10:00 Beijing = 02:00 UTC,
# MarketPhase.INTRADAY) so the market-phase guard never skips and the assertions are
# independent of the wall-clock time the test is run at.
NOW = datetime(2026, 8, 27, 2, 0, 0, tzinfo=timezone.utc)


def _ts(minutes_ago: int) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


def test_volume_spike_rt_in_realtime_alert_types():
    assert "volume_spike_rt" in REALTIME_ALERT_TYPES


def test_normalize_uses_defaults_when_missing():
    params = normalize_realtime_indicator_parameters("volume_spike_rt", {})
    assert params["window_minutes"] == DEFAULT_VOLUME_SPIKE_RT_WINDOW_MINUTES
    assert params["min_ratio"] == DEFAULT_VOLUME_SPIKE_RT_MIN_RATIO
    assert params["min_slope"] == DEFAULT_VOLUME_SPIKE_RT_MIN_SLOPE


def test_normalize_rejects_invalid_window():
    with pytest.raises(ValueError):
        normalize_realtime_indicator_parameters("volume_spike_rt", {"window_minutes": 0})
    with pytest.raises(ValueError):
        normalize_realtime_indicator_parameters("volume_spike_rt", {"window_minutes": 999})


def test_normalize_rejects_out_of_range_ratio():
    with pytest.raises(ValueError):
        normalize_realtime_indicator_parameters("volume_spike_rt", {"min_ratio": -1})
    with pytest.raises(ValueError):
        normalize_realtime_indicator_parameters("volume_spike_rt", {"min_ratio": 999})


def test_normalize_rejects_negative_slope():
    with pytest.raises(ValueError):
        normalize_realtime_indicator_parameters("volume_spike_rt", {"min_slope": -0.1})


def test_threshold_helper_returns_min_ratio():
    params = normalize_realtime_indicator_parameters("volume_spike_rt", {})
    assert threshold_for_realtime_indicator("volume_spike_rt", params) == DEFAULT_VOLUME_SPIKE_RT_MIN_RATIO


def test_threshold_helper_unknown_type_returns_none():
    assert threshold_for_realtime_indicator("some_other_type", {}) is None


def test_evaluator_no_data_does_not_trigger():
    cache = RealtimeVolumeCache(retention_minutes=30)
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache, now=NOW
    )
    assert outcome.triggered is False
    assert "数据不足" in outcome.summary or "采样不足" in outcome.summary or "未达阈值" in outcome.summary


def test_evaluator_below_ratio_floor_does_not_trigger():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 1.0)
    cache.record("600519.SH", 1.1)
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache, now=NOW
    )
    # 绝对量比 1.1x < 阈值 1.5x，且无斜率/峰值信号 → 不触发（OR 语义下也不应触发）
    assert outcome.triggered is False
    assert outcome.latest_value == pytest.approx(1.1)
    assert "量比" in outcome.summary


def test_evaluator_steep_positive_slope_triggers():
    cache = RealtimeVolumeCache(retention_minutes=30)
    # 30-minute window rising 1.0x → 2.5x → slope +0.05/min → +0.25/5min
    cache.record("600519.SH", 1.0, at=_ts(30))
    cache.record("600519.SH", 1.5, at=_ts(20))
    cache.record("600519.SH", 2.0, at=_ts(10))
    cache.record("600519.SH", 2.5, at=_ts(0))
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache, now=NOW
    )
    assert outcome.triggered is True
    assert outcome.latest_value == pytest.approx(2.5)
    assert outcome.slope == pytest.approx(0.05, abs=1e-6)
    assert "放量预警" in outcome.summary


def test_evaluator_flat_curve_does_not_trigger():
    cache = RealtimeVolumeCache(retention_minutes=30)
    # 量比稳定在 1.2x（低于绝对阈值 1.5x）且斜率为 0 → 不触发。
    # 注意：OR 语义下“绝对量比已抬升(>=1.5)”本身就会触发，这里用低于阈值的数据
    # 来真正验证“无加速度/无突发”路径不会误触发。
    cache.record("600519.SH", 1.2, at=_ts(30))
    cache.record("600519.SH", 1.2, at=_ts(15))
    cache.record("600519.SH", 1.2, at=_ts(0))
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache, now=NOW
    )
    assert outcome.triggered is False


def test_evaluator_declining_curve_does_not_trigger():
    cache = RealtimeVolumeCache(retention_minutes=30)
    # 量比从 1.4x 回落到 1.2x（均低于绝对阈值 1.5x）且斜率为负 → 不触发。
    cache.record("600519.SH", 1.4, at=_ts(30))
    cache.record("600519.SH", 1.3, at=_ts(15))
    cache.record("600519.SH", 1.2, at=_ts(0))
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache, now=NOW
    )
    assert outcome.triggered is False


def _triggering_slope_cache() -> RealtimeVolumeCache:
    """Return a cache with a steep positive slope that triggers the slope leg."""
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 1.0, at=_ts(30))
    cache.record("600519.SH", 1.5, at=_ts(20))
    cache.record("600519.SH", 2.0, at=_ts(10))
    cache.record("600519.SH", 2.5, at=_ts(0))
    return cache


def test_evaluator_with_rising_quote_shows_volume_spike_up():
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt",
        "600519.SH",
        {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25},
        _triggering_slope_cache(),
        now=NOW,
        quote=SimpleNamespace(change_pct=2.5),
    )
    assert outcome.triggered is True
    assert "放量上涨" in outcome.summary
    assert "+2.50%" in outcome.summary


def test_evaluator_with_falling_quote_shows_volume_spike_down():
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt",
        "600519.SH",
        {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25},
        _triggering_slope_cache(),
        now=NOW,
        quote=SimpleNamespace(change_pct=-1.8),
    )
    assert outcome.triggered is True
    assert "放量下跌" in outcome.summary
    assert "-1.80%" in outcome.summary


def test_evaluator_with_flat_quote_shows_volume_spike_flat():
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt",
        "600519.SH",
        {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25},
        _triggering_slope_cache(),
        now=NOW,
        quote=SimpleNamespace(change_pct=0.0),
    )
    assert outcome.triggered is True
    assert "放量平盘" in outcome.summary


def test_evaluator_unknown_type_raises():
    cache = RealtimeVolumeCache(retention_minutes=30)
    with pytest.raises(ValueError):
        evaluate_realtime_indicator_alert("some_other_type", "X.SH", {}, cache)