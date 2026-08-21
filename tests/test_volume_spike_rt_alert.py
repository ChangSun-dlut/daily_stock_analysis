"""Tests for ``volume_spike_rt`` realtime indicator normalization + evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def _ts(minutes_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


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
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache
    )
    assert outcome.triggered is False
    assert "数据不足" in outcome.summary or "采样不足" in outcome.summary or "未达阈值" in outcome.summary


def test_evaluator_below_ratio_floor_does_not_trigger():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 1.0)
    cache.record("600519.SH", 1.1)
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache
    )
    assert outcome.triggered is False
    assert "未达阈值" in outcome.summary


def test_evaluator_steep_positive_slope_triggers():
    cache = RealtimeVolumeCache(retention_minutes=30)
    # 30-minute window rising 1.0x → 2.5x → slope +0.05/min → +0.25/5min
    cache.record("600519.SH", 1.0, at=_ts(30))
    cache.record("600519.SH", 1.5, at=_ts(20))
    cache.record("600519.SH", 2.0, at=_ts(10))
    cache.record("600519.SH", 2.5, at=_ts(0))
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache
    )
    assert outcome.triggered is True
    assert outcome.latest_value == pytest.approx(2.5)
    assert outcome.slope == pytest.approx(0.05, abs=1e-6)
    assert "放量预警" in outcome.summary


def test_evaluator_flat_curve_does_not_trigger():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 2.5, at=_ts(30))
    cache.record("600519.SH", 2.5, at=_ts(15))
    cache.record("600519.SH", 2.5, at=_ts(0))
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache
    )
    assert outcome.triggered is False
    assert "未达阈值" in outcome.summary


def test_evaluator_declining_curve_does_not_trigger():
    cache = RealtimeVolumeCache(retention_minutes=30)
    cache.record("600519.SH", 3.0, at=_ts(30))
    cache.record("600519.SH", 2.5, at=_ts(15))
    cache.record("600519.SH", 2.0, at=_ts(0))
    outcome = evaluate_realtime_indicator_alert(
        "volume_spike_rt", "600519.SH", {"window_minutes": 30, "min_ratio": 1.5, "min_slope": 0.25}, cache
    )
    assert outcome.triggered is False


def test_evaluator_unknown_type_raises():
    cache = RealtimeVolumeCache(retention_minutes=30)
    with pytest.raises(ValueError):
        evaluate_realtime_indicator_alert("some_other_type", "X.SH", {}, cache)