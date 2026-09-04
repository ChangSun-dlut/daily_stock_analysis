# -*- coding: utf-8 -*-
"""Technical indicator alert helpers for AlertService P5 rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from math import isfinite
from typing import Any, Dict, Optional

import pandas as pd

from data_provider.base import normalize_stock_code

from src.core.trading_calendar import (
    MarketPhase,
    get_market_for_stock,
    infer_market_phase,
)


TECHNICAL_ALERT_TYPES = frozenset({
    "ma_price_cross",
    "rsi_threshold",
    "macd_cross",
    "kdj_cross",
    "cci_threshold",
})

# Realtime alert types: depend on intraday snapshots rather than daily K-line.
# These are evaluated against ``src.services.realtime_volume_cache`` (and any
# future realtime indicator caches) instead of the daily DataFetcher path.
REALTIME_ALERT_TYPES = frozenset({
    "volume_spike_rt",
})

# Defaults for ``volume_spike_rt`` rules. Min ratio floor guards against
# illiquid names with structurally low volume triggering on noise; min slope
# matches the "30-min window rising 1.0x → 2.5x" pattern observed in early-
# morning surges (e.g. 生益科技 600183 intraday 09:30-10:00 spike).
DEFAULT_VOLUME_SPIKE_RT_WINDOW_MINUTES = 30
DEFAULT_VOLUME_SPIKE_RT_MIN_RATIO = 1.5
DEFAULT_VOLUME_SPIKE_RT_MIN_SLOPE = 0.25  # volume_ratio per 5 minutes
MIN_VOLUME_SPIKE_RT_WINDOW_MINUTES = 1
MAX_VOLUME_SPIKE_RT_WINDOW_MINUTES = 120
MIN_VOLUME_SPIKE_RT_MIN_RATIO = 0.5
MAX_VOLUME_SPIKE_RT_MIN_RATIO = 20.0
MIN_VOLUME_SPIKE_RT_MIN_SLOPE = 0.0
MAX_VOLUME_SPIKE_RT_MIN_SLOPE = 5.0
MIN_VOLUME_SPIKE_RT_MIN_PEAK_RATIO = 0.0
MAX_VOLUME_SPIKE_RT_MIN_PEAK_RATIO = 20.0

ABOVE_BELOW_DIRECTIONS = frozenset({"above", "below"})
CROSS_DIRECTIONS = frozenset({"bullish_cross", "bearish_cross"})
MAX_REQUESTED_DAYS = 365


@dataclass
class TechnicalIndicatorAlert:
    stock_code: str
    alert_type: str
    indicator_params: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IndicatorEvaluation:
    status: str
    observed_value: Optional[float]
    threshold: Optional[float]
    message: str
    data_timestamp: Optional[datetime] = None


def normalize_indicator_parameters(alert_type: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")

    if alert_type == "ma_price_cross":
        normalized = {
            "direction": _direction(parameters.get("direction"), ABOVE_BELOW_DIRECTIONS, default="above"),
            "window": _int_in_range(parameters.get("window"), "window", default=20),
        }
        return _ensure_required_bars_fetchable(alert_type, normalized)
    if alert_type == "rsi_threshold":
        normalized = {
            "direction": _direction(parameters.get("direction"), ABOVE_BELOW_DIRECTIONS, default="above"),
            "period": _int_in_range(parameters.get("period"), "period", default=12),
            "threshold": _float_in_range(parameters.get("threshold"), "threshold", minimum=0.0, maximum=100.0),
        }
        return _ensure_required_bars_fetchable(alert_type, normalized)
    if alert_type == "macd_cross":
        fast_period = _int_in_range(parameters.get("fast_period"), "fast_period", default=12)
        slow_period = _int_in_range(parameters.get("slow_period"), "slow_period", default=26)
        if fast_period >= slow_period:
            raise ValueError("fast_period must be < slow_period")
        normalized = {
            "direction": _direction(parameters.get("direction"), CROSS_DIRECTIONS, default="bullish_cross"),
            "fast_period": fast_period,
            "slow_period": slow_period,
            "signal_period": _int_in_range(parameters.get("signal_period"), "signal_period", default=9),
        }
        return _ensure_required_bars_fetchable(alert_type, normalized)
    if alert_type == "kdj_cross":
        normalized = {
            "direction": _direction(parameters.get("direction"), CROSS_DIRECTIONS, default="bullish_cross"),
            "period": _int_in_range(parameters.get("period"), "period", default=9),
            "k_period": _int_in_range(parameters.get("k_period"), "k_period", default=3),
            "d_period": _int_in_range(parameters.get("d_period"), "d_period", default=3),
        }
        return _ensure_required_bars_fetchable(alert_type, normalized)
    if alert_type == "cci_threshold":
        normalized = {
            "direction": _direction(parameters.get("direction"), ABOVE_BELOW_DIRECTIONS, default="above"),
            "period": _int_in_range(parameters.get("period"), "period", default=14),
            "threshold": _finite_float(parameters.get("threshold"), "threshold"),
        }
        return _ensure_required_bars_fetchable(alert_type, normalized)
    raise ValueError(f"unsupported technical alert_type: {alert_type}")


def normalize_realtime_indicator_parameters(
    alert_type: str, parameters: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate and normalize parameters for ``REALTIME_ALERT_TYPES``.

    Separate from :func:`normalize_indicator_parameters` because realtime
    indicators do not require any daily bars and have different defaults.
    """
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")

    if alert_type == "volume_spike_rt":
        return {
            "window_minutes": _int_in_range(
                parameters.get("window_minutes"),
                "window_minutes",
                default=DEFAULT_VOLUME_SPIKE_RT_WINDOW_MINUTES,
                minimum=MIN_VOLUME_SPIKE_RT_WINDOW_MINUTES,
                maximum=MAX_VOLUME_SPIKE_RT_WINDOW_MINUTES,
            ),
            "min_ratio": _float_in_range(
                parameters.get("min_ratio"),
                "min_ratio",
                minimum=MIN_VOLUME_SPIKE_RT_MIN_RATIO,
                maximum=MAX_VOLUME_SPIKE_RT_MIN_RATIO,
            ) if parameters.get("min_ratio") is not None else DEFAULT_VOLUME_SPIKE_RT_MIN_RATIO,
            "min_slope": _float_in_range(
                parameters.get("min_slope"),
                "min_slope",
                minimum=MIN_VOLUME_SPIKE_RT_MIN_SLOPE,
                maximum=MAX_VOLUME_SPIKE_RT_MIN_SLOPE,
            ) if parameters.get("min_slope") is not None else DEFAULT_VOLUME_SPIKE_RT_MIN_SLOPE,
            # Peak-ratio threshold catches short bursts invisible to slope. Defaults
            # to 0.0 (disabled) when not provided, mirroring the evaluator default.
            "min_peak_ratio": _float_in_range(
                parameters.get("min_peak_ratio"),
                "min_peak_ratio",
                minimum=MIN_VOLUME_SPIKE_RT_MIN_PEAK_RATIO,
                maximum=MAX_VOLUME_SPIKE_RT_MIN_PEAK_RATIO,
            ) if parameters.get("min_peak_ratio") is not None else 0.0,
        }
    raise ValueError(f"unsupported realtime alert_type: {alert_type}")


def compute_required_bars(alert_type: str, params: Dict[str, Any]) -> int:
    if alert_type == "ma_price_cross":
        return int(params["window"]) + 1
    if alert_type == "rsi_threshold":
        return int(params["period"]) + 1
    if alert_type == "macd_cross":
        return int(params["slow_period"]) + int(params["signal_period"]) + 1
    if alert_type == "kdj_cross":
        return int(params["period"]) + int(params["k_period"]) + int(params["d_period"]) + 1
    if alert_type == "cci_threshold":
        return int(params["period"]) + 1
    raise ValueError(f"unsupported technical alert_type: {alert_type}")


def compute_requested_days(alert_type: str, params: Dict[str, Any]) -> int:
    required_bars = compute_required_bars(alert_type, params)
    return min(max(required_bars * 3, required_bars + 30), MAX_REQUESTED_DAYS)


def threshold_for_indicator(alert_type: str, params: Dict[str, Any]) -> Optional[float]:
    if alert_type in {"rsi_threshold", "cci_threshold"}:
        return float(params["threshold"])
    if alert_type in {"macd_cross", "kdj_cross"}:
        return 0.0
    return None


def threshold_for_realtime_indicator(
    alert_type: str, params: Dict[str, Any]
) -> Optional[float]:
    """Display threshold for realtime indicators.

    ``volume_spike_rt`` returns ``min_ratio`` (the floor) so the UI can show
    "above 1.5x" rather than nothing.
    """
    if alert_type == "volume_spike_rt":
        return float(params.get("min_ratio", DEFAULT_VOLUME_SPIKE_RT_MIN_RATIO))
    return None


def evaluate_indicator_alert(
    alert_type: str,
    stock_code: str,
    params: Dict[str, Any],
    df: Any,
    *,
    now: Optional[datetime] = None,
) -> IndicatorEvaluation:
    columns = ("close",)
    if alert_type in {"kdj_cross", "cci_threshold"}:
        columns = ("high", "low", "close")

    try:
        normalized = normalize_ohlcv(df, required_columns=columns, now=now)
    except ValueError as exc:
        return IndicatorEvaluation(
            status="degraded",
            observed_value=None,
            threshold=threshold_for_indicator(alert_type, params),
            message=str(exc),
        )
    if normalized.empty:
        return IndicatorEvaluation(
            status="degraded",
            observed_value=None,
            threshold=threshold_for_indicator(alert_type, params),
            message="No closed daily data available",
        )
    if len(normalized) < 2:
        return IndicatorEvaluation(
            status="degraded",
            observed_value=None,
            threshold=threshold_for_indicator(alert_type, params),
            message="insufficient closed bars for edge evaluation",
            data_timestamp=_latest_timestamp(normalized),
        )

    required_bars = compute_required_bars(alert_type, params)
    if len(normalized) < required_bars:
        return IndicatorEvaluation(
            status="degraded",
            observed_value=None,
            threshold=threshold_for_indicator(alert_type, params),
            message=f"insufficient data: need {required_bars} bars, got {len(normalized)}",
            data_timestamp=_latest_timestamp(normalized),
        )

    if alert_type == "ma_price_cross":
        return _evaluate_ma(stock_code, params, normalized)
    if alert_type == "rsi_threshold":
        return _evaluate_rsi(stock_code, params, normalized)
    if alert_type == "macd_cross":
        return _evaluate_macd(stock_code, params, normalized)
    if alert_type == "kdj_cross":
        return _evaluate_kdj(stock_code, params, normalized)
    if alert_type == "cci_threshold":
        return _evaluate_cci(stock_code, params, normalized)
    raise ValueError(f"unsupported technical alert_type: {alert_type}")


def normalize_ohlcv(
    df: Any,
    *,
    required_columns: tuple[str, ...],
    now: Optional[datetime] = None,
) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    output = pd.DataFrame(index=df.index.copy())
    output["date"] = _date_series(df)

    missing = []
    for canonical in required_columns:
        source = _find_column(df, canonical)
        if source is None:
            missing.append(canonical)
            continue
        output[canonical] = pd.to_numeric(df[source], errors="coerce")
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"daily data missing {missing_text} column")

    output = output.dropna(subset=list(required_columns)).copy()
    if output.empty:
        return output
    output = _drop_partial_today(output, now=now)
    if output.empty:
        return output.reset_index(drop=True)
    output = output.sort_values(by="date", kind="stable", na_position="first").reset_index(drop=True)
    return output


def _evaluate_ma(stock_code: str, params: Dict[str, Any], df: pd.DataFrame) -> IndicatorEvaluation:
    window = int(params["window"])
    direction = str(params["direction"])
    series = df["close"].rolling(window=window).mean()
    latest = _latest_timestamp(df)
    prev_close, curr_close = float(df["close"].iloc[-2]), float(df["close"].iloc[-1])
    prev_ma, curr_ma = float(series.iloc[-2]), float(series.iloc[-1])
    if not all(isfinite(value) for value in (prev_ma, curr_ma)):
        return _indicator_unavailable("MA", latest)

    prev_delta = prev_close - prev_ma
    curr_delta = curr_close - curr_ma
    triggered = _crossed_zero(prev_delta, curr_delta, direction)
    message = (
        f"{stock_code} close {curr_close:.4f} crossed {direction} MA{window} {curr_ma:.4f}"
        if triggered
        else f"{stock_code} close {curr_close:.4f} did not edge-cross {direction} MA{window} {curr_ma:.4f}"
    )
    return IndicatorEvaluation(
        status="triggered" if triggered else "not_triggered",
        observed_value=curr_close,
        threshold=curr_ma,
        message=message,
        data_timestamp=latest,
    )


def _evaluate_rsi(stock_code: str, params: Dict[str, Any], df: pd.DataFrame) -> IndicatorEvaluation:
    period = int(params["period"])
    threshold = float(params["threshold"])
    direction = str(params["direction"])
    rsi = _calculate_rsi(df["close"], period)
    latest = _latest_timestamp(df)
    prev_value, curr_value = float(rsi.iloc[-2]), float(rsi.iloc[-1])
    if not all(isfinite(value) for value in (prev_value, curr_value)):
        return _indicator_unavailable("RSI", latest, threshold=threshold)

    triggered = _crossed_threshold(prev_value, curr_value, threshold, direction)
    message = (
        f"{stock_code} RSI{period} {curr_value:.2f} crossed {direction} {threshold:.2f}"
        if triggered
        else f"{stock_code} RSI{period} {curr_value:.2f} did not edge-cross {direction} {threshold:.2f}"
    )
    return IndicatorEvaluation(
        status="triggered" if triggered else "not_triggered",
        observed_value=curr_value,
        threshold=threshold,
        message=message,
        data_timestamp=latest,
    )


def _evaluate_macd(stock_code: str, params: Dict[str, Any], df: pd.DataFrame) -> IndicatorEvaluation:
    fast_period = int(params["fast_period"])
    slow_period = int(params["slow_period"])
    signal_period = int(params["signal_period"])
    direction = str(params["direction"])
    ema_fast = df["close"].ewm(span=fast_period, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow_period, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal_period, adjust=False).mean()
    delta = dif - dea
    latest = _latest_timestamp(df)
    prev_delta, curr_delta = float(delta.iloc[-2]), float(delta.iloc[-1])
    if not all(isfinite(value) for value in (prev_delta, curr_delta)):
        return _indicator_unavailable("MACD", latest, threshold=0.0)

    triggered = _crossed_cross_direction(prev_delta, curr_delta, direction)
    message = (
        f"{stock_code} MACD DIF/DEA {direction}: delta = {curr_delta:.4f}"
        if triggered
        else f"{stock_code} MACD delta {curr_delta:.4f} did not edge-cross {direction}"
    )
    return IndicatorEvaluation(
        status="triggered" if triggered else "not_triggered",
        observed_value=curr_delta,
        threshold=0.0,
        message=message,
        data_timestamp=latest,
    )


def _evaluate_kdj(stock_code: str, params: Dict[str, Any], df: pd.DataFrame) -> IndicatorEvaluation:
    period = int(params["period"])
    k_period = int(params["k_period"])
    d_period = int(params["d_period"])
    direction = str(params["direction"])
    lowest_low = df["low"].rolling(window=period).min()
    highest_high = df["high"].rolling(window=period).max()
    denominator = highest_high - lowest_low
    rsv = ((df["close"] - lowest_low) / denominator.mask(denominator == 0) * 100).fillna(50)
    k_value = rsv.ewm(alpha=1 / k_period, adjust=False).mean()
    d_value = k_value.ewm(alpha=1 / d_period, adjust=False).mean()
    delta = k_value - d_value
    latest = _latest_timestamp(df)
    prev_delta, curr_delta = float(delta.iloc[-2]), float(delta.iloc[-1])
    if not all(isfinite(value) for value in (prev_delta, curr_delta)):
        return _indicator_unavailable("KDJ", latest, threshold=0.0)

    triggered = _crossed_cross_direction(prev_delta, curr_delta, direction)
    message = (
        f"{stock_code} KDJ K/D {direction}: delta = {curr_delta:.4f}"
        if triggered
        else f"{stock_code} KDJ delta {curr_delta:.4f} did not edge-cross {direction}"
    )
    return IndicatorEvaluation(
        status="triggered" if triggered else "not_triggered",
        observed_value=curr_delta,
        threshold=0.0,
        message=message,
        data_timestamp=latest,
    )


def _evaluate_cci(stock_code: str, params: Dict[str, Any], df: pd.DataFrame) -> IndicatorEvaluation:
    period = int(params["period"])
    threshold = float(params["threshold"])
    direction = str(params["direction"])
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    tp_ma = typical_price.rolling(window=period).mean()
    mean_deviation = typical_price.rolling(window=period).apply(
        lambda values: float(abs(values - values.mean()).mean()),
        raw=False,
    )
    cci = (typical_price - tp_ma) / (0.015 * mean_deviation.mask(mean_deviation == 0))
    latest = _latest_timestamp(df)
    prev_value, curr_value = float(cci.iloc[-2]), float(cci.iloc[-1])
    if not all(isfinite(value) for value in (prev_value, curr_value)):
        return _indicator_unavailable("CCI", latest, threshold=threshold)

    triggered = _crossed_threshold(prev_value, curr_value, threshold, direction)
    message = (
        f"{stock_code} CCI{period} {curr_value:.2f} crossed {direction} {threshold:.2f}"
        if triggered
        else f"{stock_code} CCI{period} {curr_value:.2f} did not edge-cross {direction} {threshold:.2f}"
    )
    return IndicatorEvaluation(
        status="triggered" if triggered else "not_triggered",
        observed_value=curr_value,
        threshold=threshold,
        message=message,
        data_timestamp=latest,
    )


def _ensure_required_bars_fetchable(alert_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    required_bars = compute_required_bars(alert_type, params)
    if required_bars > MAX_REQUESTED_DAYS:
        raise ValueError(
            f"{alert_type} periods require {required_bars} bars, "
            f"but at most {MAX_REQUESTED_DAYS} days can be requested"
        )
    return params


def _direction(value: Any, allowed: frozenset[str], *, default: str) -> str:
    direction = str(value or default).strip().lower()
    if direction not in allowed:
        raise ValueError(f"invalid direction: {direction}")
    return direction


def _int_in_range(value: Any, field_name: str, *, default: int, minimum: int = 2, maximum: int = 250) -> int:
    raw_value = default if value is None or value == "" else value
    try:
        number = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}: {value}") from exc
    if str(raw_value).strip() not in {str(number), f"{number}.0"}:
        raise ValueError(f"{field_name} must be an integer")
    if number < minimum or number > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return number


def _finite_float(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}: {value}") from exc
    if not isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _float_in_range(
    value: Any,
    field_name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    number = _finite_float(value, field_name)
    if number < minimum or number > maximum:
        raise ValueError(f"{field_name} must be between {minimum:g} and {maximum:g}")
    return number


def _calculate_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    # 使用 Wilder's EMA / SMMA 口径，不使用 rolling SMA。
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return (100 - (100 / (1 + rs))).fillna(50)


def _crossed_threshold(prev_value: float, curr_value: float, threshold: float, direction: str) -> bool:
    if direction == "above":
        return prev_value <= threshold < curr_value
    if direction == "below":
        return prev_value >= threshold > curr_value
    return False


def _crossed_zero(prev_delta: float, curr_delta: float, direction: str) -> bool:
    if direction == "above":
        return prev_delta <= 0 < curr_delta
    if direction == "below":
        return prev_delta >= 0 > curr_delta
    return False


def _crossed_cross_direction(prev_delta: float, curr_delta: float, direction: str) -> bool:
    if direction == "bullish_cross":
        return prev_delta <= 0 < curr_delta
    if direction == "bearish_cross":
        return prev_delta >= 0 > curr_delta
    return False


def _indicator_unavailable(
    indicator_name: str,
    data_timestamp: Optional[datetime],
    *,
    threshold: Optional[float] = None,
) -> IndicatorEvaluation:
    return IndicatorEvaluation(
        status="degraded",
        observed_value=None,
        threshold=threshold,
        message=f"{indicator_name} value is not available",
        data_timestamp=data_timestamp,
    )


def _find_column(df: pd.DataFrame, canonical: str) -> Optional[Any]:
    candidates = {
        "date": ("date", "trade_date", "datetime", "time", "日期", "交易日期"),
        "open": ("open", "open_price", "开盘", "开盘价"),
        "high": ("high", "high_price", "最高", "最高价"),
        "low": ("low", "low_price", "最低", "最低价"),
        "close": ("close", "close_price", "收盘", "收盘价"),
        "volume": ("volume", "vol", "成交量"),
    }
    by_normalized = {str(column).strip().lower(): column for column in df.columns}
    for candidate in candidates[canonical]:
        column = by_normalized.get(candidate.lower())
        if column is not None:
            return column
    return None


def _date_series(df: pd.DataFrame) -> pd.Series:
    date_column = _find_column(df, "date")
    if date_column is not None:
        return pd.to_datetime(df[date_column], errors="coerce")
    index = df.index
    if isinstance(index, pd.DatetimeIndex):
        return pd.Series(index.to_pydatetime(), index=df.index)
    return pd.Series([pd.NaT] * len(df), index=df.index)


def _drop_partial_today(df: pd.DataFrame, *, now: Optional[datetime] = None) -> pd.DataFrame:
    current = now or datetime.now()
    if current.time() >= time(16, 0):
        return df
    try:
        parsed = pd.to_datetime(df["date"].iloc[-1], errors="coerce")
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return df.iloc[:-1].copy()
    if pd.isna(parsed):
        return df.iloc[:-1].copy()
    last_date = parsed.date()
    if last_date == current.date():
        return df.iloc[:-1].copy()
    return df


def _latest_timestamp(df: pd.DataFrame) -> Optional[datetime]:
    try:
        raw_value = df["date"].iloc[-1]
        if pd.isna(raw_value):
            return None
        parsed = pd.to_datetime(raw_value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None


# ---------------------------------------------------------------- realtime
@dataclass(frozen=True)
class RealtimeIndicatorOutcome:
    """Result of evaluating a realtime indicator rule.

    ``triggered`` mirrors ``IndicatorOutcome.triggered``; ``summary`` is a
    short string the notification template can use directly (e.g.
    "量比 1.82x，30分钟斜率 +0.31/5min")."""

    triggered: bool
    summary: str
    latest_value: Optional[float] = None
    slope: Optional[float] = None
    window_points: int = 0
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    grade: Optional[str] = None            # 信号分级：强/中/弱
    stale_seconds: Optional[float] = None  # 行情延迟（秒）
    data_channel: Optional[str] = None     # 行情来源 token（如 pytdx）
    direction: Optional[str] = None        # 涨跌方向：up/down/flat


_SOURCE_FRIENDLY = {
    "pytdx": "通达信",
    "efinance": "东方财富",
    "akshare_em": "东方财富",
    "akshare_sina": "新浪",
    "akshare_qq": "腾讯",
    "tencent": "腾讯",
    "tushare": "Tushare",
    "sina": "新浪",
    "hithink": "同花顺",
}


def _friendly_channel(token: Optional[Any]) -> str:
    """Map a RealtimeSource token (or enum) to a friendly Chinese name."""
    if token is None:
        return ""
    value = token.value if hasattr(token, "value") else token
    if not isinstance(value, str):
        return ""
    return _SOURCE_FRIENDLY.get(value, value)


def _grade_label(n_hits: int) -> Optional[str]:
    """Map number of independent signal hits to a confidence grade."""
    if n_hits >= 3:
        return "强"
    if n_hits == 2:
        return "中"
    if n_hits == 1:
        return "弱"
    return None


def _channel_stale_suffix(quote: Optional[Any]) -> str:
    """Build a '｜通达信·延迟Ns' suffix for notification text."""
    if quote is None:
        return ""
    ch = _friendly_channel(getattr(quote, "source", None))
    stale = getattr(quote, "stale_seconds", None)
    parts: List[str] = []
    if ch:
        parts.append(ch)
    if stale is not None:
        try:
            parts.append(f"延迟{int(round(stale))}s")
        except (TypeError, ValueError):
            pass
    return "｜" + "·".join(parts) if parts else ""


def _quote_pct(quote: Optional[Any]) -> Optional[float]:
    """Return the effective change_pct for a quote, computing from price/pre_close if needed."""
    if quote is None:
        return None
    pct = getattr(quote, "change_pct", None)
    if pct is not None and isfinite(pct):
        return float(pct)
    pct = getattr(quote, "pct_change", None)
    if pct is not None and isfinite(pct):
        return float(pct)
    price = getattr(quote, "price", None)
    pre_close = getattr(quote, "pre_close", None)
    if price is not None and pre_close is not None:
        try:
            price_f = float(price)
            pre_f = float(pre_close)
            if pre_f:
                return (price_f - pre_f) / pre_f * 100
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return None


def _quote_direction(quote: Optional[Any]) -> Optional[str]:
    """Return 'up'/'down'/'flat' for a realtime quote, or None if unavailable."""
    pct = _quote_pct(quote)
    if pct is None:
        return None
    if pct > 0:
        return "up"
    if pct < 0:
        return "down"
    return "flat"


def _format_volume_direction(quote: Optional[Any]) -> str:
    """Return a human-readable up/down label for a realtime volume-spike quote."""
    pct = _quote_pct(quote)
    if pct is None:
        return "放量预警"
    if pct > 0:
        return f"放量上涨预警（+{pct:.2f}%）"
    if pct < 0:
        return f"放量下跌预警（{pct:.2f}%）"
    return "放量平盘预警（0.00%）"


def evaluate_realtime_indicator_alert(
    alert_type: str,
    stock_code: str,
    params: Dict[str, Any],
    cache: Any,
    *,
    now: Optional[datetime] = None,
    quote: Optional[Any] = None,
) -> RealtimeIndicatorOutcome:
    """Evaluate a realtime indicator against the supplied cache.

    ``cache`` is expected to expose ``current_ratio`` and ``slope`` (both
    accept ``window_minutes`` and ``now`` kwargs). The default singleton from
    :mod:`src.services.realtime_volume_cache` satisfies this contract.

    ``quote`` is an optional realtime quote object (e.g.
    :class:`data_provider.realtime_types.UnifiedRealtimeQuote`) used to enrich
    the triggered summary with up/down direction.
    """
    if alert_type not in REALTIME_ALERT_TYPES:
        raise ValueError(f"unsupported realtime alert_type: {alert_type}")

    # Normalise stock code early so market-phase inference can resolve the market.
    stock_code = normalize_stock_code(stock_code)

    # Realtime alerts only make sense while the market is actively trading.
    # We fail-open: only skip when we can positively identify a non-active phase.
    now_dt = now or datetime.now(timezone.utc)
    market = get_market_for_stock(stock_code)
    if market:
        phase = infer_market_phase(market, current_time=now_dt)
        if phase in {
            MarketPhase.PREMARKET,
            MarketPhase.LUNCH_BREAK,
            MarketPhase.POSTMARKET,
            MarketPhase.NON_TRADING,
        }:
            return RealtimeIndicatorOutcome(
                triggered=False,
                summary=f"市场未开盘（{phase.value}），跳过实时指标检查",
                latest_value=None,
                slope=None,
                window_points=0,
                evaluated_at=now_dt,
            )

    if alert_type == "volume_spike_rt":
        window = int(params["window_minutes"])
        min_ratio = float(params["min_ratio"])
        min_slope = float(params.get("min_slope", 0.0))
        # Peak-ratio threshold for catching *short* bursts (e.g. a 3-minute
        # explosion) that are invisible to a 30-min slope and may be missed by
        # the 5-min sampling interval. Disabled when <= 0.
        min_peak = float(params.get("min_peak_ratio", 0.0))
        current = cache.current_ratio(stock_code, now=now_dt)
        if current is None:
            return RealtimeIndicatorOutcome(
                triggered=False,
                summary="量比数据不足",
                latest_value=current,
                slope=None,
                window_points=0,
                evaluated_at=now_dt,
            )

        slope = cache.slope(stock_code, window_minutes=window, now=now_dt)
        # slope units = volume_ratio per minute; convert to per-5-min for display
        slope_per_5min = slope * 5.0 if slope is not None else None
        peak = cache.peak_ratio(stock_code, window_minutes=window, now=now_dt)

        # Three independent signals — OR semantics so a stock "heating up" is
        # caught even before its absolute volume ratio crosses the threshold:
        #   1) absolute: ratio already elevated (>= min_ratio)
        #   2) acceleration: slope over the window is steep (ratio expanding)
        #      with a meaningful base (current > 1.0) to avoid trivial noise.
        #   3) peak: the window's highest ratio spiked (short sudden burst)
        ratio_ok = current >= (min_ratio - 1e-9)
        slope_ok = (
            slope is not None
            and min_slope > 0
            and slope_per_5min >= (min_slope - 1e-9)
            and current > 1.0
        )
        peak_ok = peak is not None and min_peak > 0 and peak >= (min_peak - 1e-9)

        # --- signal grading (#6): count how many independent conditions fired ---
        hits = []
        if ratio_ok:
            hits.append("量比")
        if slope_ok:
            hits.append("斜率")
        if peak_ok:
            hits.append("峰值")

        grade = _grade_label(len(hits))
        channel_suffix = _channel_stale_suffix(quote)
        stale_seconds = getattr(quote, "stale_seconds", None) if quote is not None else None
        src = getattr(quote, "source", None) if quote is not None else None
        data_channel = src.value if hasattr(src, "value") else (src if isinstance(src, str) else None)
        direction = _quote_direction(quote)

        if ratio_ok or slope_ok or peak_ok:
            triggered = True
            direction_label = _format_volume_direction(quote)
            head = (
                f"{direction_label}：{stock_code} 量比 {current:.2f}x"
                + (f"（窗口峰值 {peak:.2f}x）" if peak is not None else "")
            )
            detail = f"，{window} 分钟斜率 +{slope_per_5min:.2f}/5min" if slope_per_5min is not None else ""
            summary = (
                f"{head}{detail}"
                f"（命中：{'/'.join(hits)}；阈值 量比≥{min_ratio:.2f} 或 斜率≥{min_slope:.2f} 或 峰值≥{min_peak:.2f}）"
            )
        else:
            triggered = False
            summary = f"量比 {current:.2f}x"
            if slope_per_5min is not None:
                summary += f"，斜率 +{slope_per_5min:.2f}/5min"
            if peak is not None:
                summary += f"，峰值 {peak:.2f}x"
            summary += f"（阈值 量比≥{min_ratio:.2f}/斜率≥{min_slope:.2f}/峰值≥{min_peak:.2f}）"
        summary = summary + (f"｜信号强度 {grade}" if grade else "") + channel_suffix
        return RealtimeIndicatorOutcome(
            triggered=triggered,
            summary=summary,
            latest_value=current,
            slope=slope,
            window_points=cache.size(stock_code),
            evaluated_at=now_dt,
            grade=grade,
            stale_seconds=stale_seconds,
            data_channel=data_channel,
            direction=direction,
        )
    raise ValueError(f"unsupported realtime alert_type: {alert_type}")
