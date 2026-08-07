"""一键回测 — 对选股结果进行近期形态验证与多时间窗口盈亏回测。

依赖：yfinance / numpy / pandas
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from src.stock_analyzer import StockTrendAnalyzer

logger = logging.getLogger(__name__)

# 策略阈值
RANGE_20D_MAX = 20.0
VOLATILITY_20D_MAX = 35.0
CONSOLIDATION_DAYS_MIN = 6
CHANGE_20D_MAX = 10.0
LOOKBACK_TRADING_DAYS = 60


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class PatternCheck:
    range_20d_pct: float = 0
    volatility_20d_pct: float = 0
    change_20d_pct: float = 0
    consolidation_days: int = 0
    volume_ratio: float = 0
    signal_day_return_pct: float = 0
    all_pass: bool = False
    violations: list[str] = field(default_factory=list)


@dataclass
class HoldingReturn:
    days: int = 0
    return_pct: float = 0


@dataclass
class MaSnapshot:
    ma: int = 0
    value: float = 0
    price_above: bool = False


@dataclass
class WindowSimulation:
    signal_date: str = ""
    buy_price: float = 0
    hold_days: int = 0
    hold_return_pct: float = 0
    pattern_all_pass: bool = False
    range_20d: float = 0
    volatility_20d: float = 0
    change_20d: float = 0
    consolidation_days: int = 0


@dataclass
class CandidateBacktestResult:
    code: str = ""
    name: str = ""
    signal_date: str = ""
    signal_price: float = 0
    pattern: PatternCheck | None = None
    holding_returns: list[HoldingReturn] = field(default_factory=list)
    max_return_5d_pct: float | None = None
    min_return_5d_pct: float | None = None
    ma_snapshots: list[MaSnapshot] = field(default_factory=list)
    window_simulations: list[WindowSimulation] = field(default_factory=list)
    error: str | None = None


@dataclass
class BacktestResponse:
    strategy: str = ""
    signal_date: str = ""
    candidates: list[CandidateBacktestResult] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _a_stock_ticker(code: str) -> str:
    """Convert a bare A-share code to a yfinance-compatible ticker.

    Returns:
        - ``{code}.SS`` for Shanghai (``60xxxx``, ``68xxxx``)
        - ``{code}.SZ`` for Shenzhen (``00xxxx``, ``30xxxx``)
        - ``{code}.BJ`` for Beijing Stock Exchange (``4xxxxx``/``8xxxxx``/``92xxxx``)

    Note: Yahoo Finance currently does not serve BSE daily data; the call will
    still return an empty frame, but we surface a clearer error downstream.
    """
    bare = code.upper().lstrip(".")
    # Normalise bare codes that already carry a suffix
    for suffix in (".SS", ".SH", ".SZ", ".BJ"):
        if bare.endswith(suffix):
            return bare

    if bare.startswith(("60", "68")):
        return f"{bare}.SS"
    if bare.startswith(("8", "4", "92")):
        # Beijing Stock Exchange (北交所)
        return f"{bare}.BJ"
    # 000xxx, 002xxx, 003xxx, 300xxx, 301xxx ...
    return f"{bare}.SZ"


def _round(val: float, ndigits: int = 2) -> float:
    return round(float(val), ndigits)


def _ensure_df(raw: Any) -> pd.DataFrame | None:
    """把 yfinance 返回的 DataFrame 规整化。"""
    if raw is None or (hasattr(raw, "empty") and raw.empty):
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    # 统一列名
    col_map = {}
    for c in raw.columns:
        cl = str(c).lower().strip()
        if cl in ("date", "datetime"):
            col_map[c] = "date"
        elif cl in ("open",):
            col_map[c] = "open"
        elif cl in ("high",):
            col_map[c] = "high"
        elif cl in ("low",):
            col_map[c] = "low"
        elif cl in ("close",):
            col_map[c] = "close"
        elif cl in ("volume",):
            col_map[c] = "volume"
    raw = raw.rename(columns=col_map)
    for col in ["open", "high", "low", "close", "volume"]:
        if col in raw.columns:
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
    if "date" in raw.columns:
        raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw = raw.dropna(subset=["close"])
    if raw.empty:
        return None
    return raw


# ---------------------------------------------------------------------------
# 核心计算
# ---------------------------------------------------------------------------
def _compute_pattern(df: pd.DataFrame, signal_idx: int) -> PatternCheck:
    pre = df.iloc[max(0, signal_idx - 20) : signal_idx]
    if len(pre) < 10:
        return PatternCheck(all_pass=False, violations=["历史数据不足（<10 日）"])

    close = pre["close"].astype(float)
    high = pre["high"].astype(float)
    low = pre["low"].astype(float)

    h20, l20 = float(high.tail(20).max()), float(low.tail(20).min())
    c_first = float(close.iloc[-20]) if len(close) >= 20 else float(close.iloc[0])
    range_20d = (h20 - l20) / c_first * 100 if c_first > 0 else 0

    returns = close.pct_change().dropna()
    vol_20d = float(returns.std() * np.sqrt(252) * 100) if len(returns) > 1 else 0

    change_20d = (float(close.iloc[-1]) / c_first - 1) * 100 if c_first > 0 else 0

    # 横盘天数估算
    r5h, r5l = float(high.tail(5).max()), float(low.tail(5).min())
    cons_days = 0
    for i in range(len(pre) - 1, -1, -1):
        if float(high.iloc[i]) <= r5h * 1.05 and float(low.iloc[i]) >= r5l * 0.95:
            cons_days += 1
        else:
            break

    vol_today = float(df.iloc[signal_idx]["volume"])
    vol_5d_avg = float(df.iloc[max(0, signal_idx - 5) : signal_idx]["volume"].mean())
    vol_ratio = vol_today / vol_5d_avg if vol_5d_avg > 0 else 1

    today_close = float(df.iloc[signal_idx]["close"])
    today_open = float(df.iloc[signal_idx]["open"])
    today_return = (today_close / today_open - 1) * 100 if today_open > 0 else 0

    violations = []
    if abs(range_20d) > RANGE_20D_MAX:
        violations.append(f"20日振幅={range_20d:.1f}%>{RANGE_20D_MAX}%")
    if vol_20d > VOLATILITY_20D_MAX:
        violations.append(f"波动率={vol_20d:.1f}%>{VOLATILITY_20D_MAX}%")
    if abs(change_20d) > CHANGE_20D_MAX:
        violations.append(f"20日涨幅={change_20d:+.1f}%，超出±{CHANGE_20D_MAX}%")
    if cons_days < CONSOLIDATION_DAYS_MIN:
        violations.append(f"横盘={cons_days}天<{CONSOLIDATION_DAYS_MIN}天")

    return PatternCheck(
        range_20d_pct=_round(range_20d),
        volatility_20d_pct=_round(vol_20d),
        change_20d_pct=_round(change_20d),
        consolidation_days=cons_days,
        volume_ratio=_round(vol_ratio),
        signal_day_return_pct=_round(today_return),
        all_pass=len(violations) == 0,
        violations=violations,
    )


def _compute_holding_returns(
    df: pd.DataFrame, signal_idx: int, buy_price: float
) -> list[HoldingReturn]:
    result = []
    for days in [1, 3, 5, 10]:
        sell_idx = signal_idx + days
        if sell_idx < len(df):
            sell_price = float(df.iloc[sell_idx]["close"])
            ret = (sell_price / buy_price - 1) * 100
            result.append(HoldingReturn(days=days, return_pct=_round(ret)))
    return result


def _compute_window_simulations(
    df: pd.DataFrame, signal_idx: int
) -> list[WindowSimulation]:
    """多时间窗口模拟：假设在更早日期触发信号，持有至今的收益。"""
    hold_to_price = float(df.iloc[signal_idx]["close"])
    simulations = []
    offsets = [3, 6, 10, 15, 20]
    # 取至多 4 个有效偏移
    valid = [o for o in offsets if signal_idx - o >= 25]
    for offset in valid[-4:]:
        idx = signal_idx - offset
        buy_price = float(df.iloc[idx]["open"])
        hold_return = (hold_to_price / buy_price - 1) * 100 if buy_price > 0 else 0
        pattern = _compute_pattern(df, idx)
        simulations.append(WindowSimulation(
            signal_date=str(df.iloc[idx]["date"])[:10],
            buy_price=_round(buy_price),
            hold_days=offset,
            hold_return_pct=_round(hold_return),
            pattern_all_pass=pattern.all_pass,
            range_20d=pattern.range_20d_pct,
            volatility_20d=pattern.volatility_20d_pct,
            change_20d=pattern.change_20d_pct,
            consolidation_days=pattern.consolidation_days,
        ))
    return simulations


def _compute_ma_snapshots(df: pd.DataFrame, idx: int) -> list[MaSnapshot]:
    close = float(df.iloc[idx]["close"])
    result = []
    for ma_days in [5, 10, 20, 60]:
        start = max(0, idx - ma_days + 1)
        segment = df.iloc[start : idx + 1]["close"].astype(float)
        if len(segment) >= ma_days:
            ma_val = float(segment.mean())
            result.append(MaSnapshot(ma=ma_days, value=_round(ma_val), price_above=close > ma_val))
    return result


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
@dataclass
class TrendSnapshot:
    """Compact trend analysis snapshot for yesterday backtest display."""
    trend_status: str = ""           # 趋势状态
    trend_strength: float = 0        # 0-100
    ma_alignment: str = ""           # 均线排列
    ma5: float = 0
    ma10: float = 0
    ma20: float = 0
    ma60: float = 0
    bias_ma5: float = 0              # 乖离率(%)
    volume_status: str = ""          # 量能状态
    volume_ratio_5d: float = 0       # 5日均量比
    macd_dif: float = 0
    macd_dea: float = 0
    macd_bar: float = 0
    macd_status: str = ""            # MACD状态
    macd_signal: str = ""
    rsi_6: float = 0
    rsi_12: float = 0
    rsi_24: float = 0
    rsi_status: str = ""             # RSI状态
    buy_signal: str = ""             # 买入信号
    signal_score: int = 0            # 综合评分


@dataclass
class YesterdayCandidateResult:
    """Single candidate result from yesterday backtest (buy at yesterday open, sell today)."""
    code: str = ""
    name: str = ""
    signal_date: str = ""          # yesterday (second-to-last trading day)
    today_date: str = ""           # today (last trading day)
    signal_price: float = 0        # yesterday close
    today_open: float = 0
    today_close: float = 0
    today_high: float = 0
    today_low: float = 0
    today_return_pct: float = 0    # today close / yesterday open - 1
    today_high_return_pct: float = 0
    today_low_return_pct: float = 0
    pattern: PatternCheck | None = None
    volume_ratio: float = 0
    has_anomaly: bool = False
    anomaly_reasons: list[str] = field(default_factory=list)
    error: str | None = None
    trend: TrendSnapshot | None = None   # 技术面分析快照


@dataclass
class YesterdayBacktestResponse:
    """Response for yesterday backtest."""
    candidates: list[YesterdayCandidateResult] = field(default_factory=list)
    total: int = 0
    anomaly_count: int = 0
    avg_return_pct: float = 0
    signal_date: str = ""
    today_date: str = ""
    errors: list[str] = field(default_factory=list)


def run_backtest(
    candidates: list[dict[str, Any]], strategy: str = "consolidation_breakout"
) -> BacktestResponse:
    if not candidates:
        return BacktestResponse(strategy=strategy, signal_date="")

    end_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=LOOKBACK_TRADING_DAYS * 2)).strftime("%Y-%m-%d")
    signal_date_default = datetime.now().strftime("%Y-%m-%d")

    results: list[CandidateBacktestResult] = []

    for c in candidates:
        code = str(c.get("code", "")).strip()
        name = str(c.get("name", "")).strip()
        signal_price = float(c.get("price", c.get("close", 0)) or 0)
        if not code:
            continue

        ticker = _a_stock_ticker(code)
        result = CandidateBacktestResult(code=code, name=name)

        try:
            raw = yf.download(ticker, start=start_date, end=end_date, progress=False, auto_adjust=True)
            df = _ensure_df(raw)
            if df is None:
                result.error = "无法获取历史数据"
                results.append(result)
                continue

            signal_idx = len(df) - 1
            result.signal_date = str(df.iloc[signal_idx]["date"])[:10]
            result.signal_price = _round(float(df.iloc[signal_idx]["close"]))

            buy_price = float(df.iloc[signal_idx]["open"])
            result.pattern = _compute_pattern(df, signal_idx)
            result.holding_returns = _compute_holding_returns(df, signal_idx, buy_price)
            result.window_simulations = _compute_window_simulations(df, signal_idx)
            result.ma_snapshots = _compute_ma_snapshots(df, signal_idx)

            # 5 日最优/最差
            future = df.iloc[signal_idx + 1 :]
            if len(future) >= 1:
                result.max_return_5d_pct = _round(
                    (float(future.iloc[:5]["high"].max()) / buy_price - 1) * 100
                )
                result.min_return_5d_pct = _round(
                    (float(future.iloc[:5]["low"].min()) / buy_price - 1) * 100
                )

        except Exception as exc:
            logger.warning("回测 %s(%s) 失败: %s", code, ticker, exc)
            result.error = str(exc)[:200]

        results.append(result)

    # 汇总
    total = len(results)
    errs = sum(1 for r in results if r.error)
    ok = total - errs
    profitable = sum(
        1 for r in results if r.holding_returns and r.holding_returns[0].return_pct > 0
    )
    pattern_ok = sum(1 for r in results if r.pattern and r.pattern.all_pass)

    summary = {
        "total_candidates": total,
        "valid_count": ok,
        "error_count": errs,
        "profitable_count": profitable,
        "pattern_pass_count": pattern_ok,
        "all_profitable": profitable == ok and ok > 0,
        "all_pattern_ok": pattern_ok == ok and ok > 0,
    }

    return BacktestResponse(
        strategy=strategy,
        signal_date=signal_date_default,
        candidates=results,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# 昨日复盘：以昨日为信号日，评估今日实际回报 & 异常报警
# ---------------------------------------------------------------------------
def run_yesterday_backtest(
    candidates: list[dict[str, Any]], strategy: str = "consolidation_breakout"
) -> YesterdayBacktestResponse:
    """Backtest each candidate using yesterday as the signal date.

    For each candidate:
    - Uses the second-to-last trading day as "yesterday" (signal day)
    - Validates the pattern at yesterday
    - Computes today's actual return (buy yesterday open → sell today close)
    - Flags anomalies: pattern failures, significant drops, data errors
    """
    results: list[YesterdayCandidateResult] = []
    errors: list[str] = []
    signal_date_default = ""
    today_date_default = ""

    end_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=LOOKBACK_TRADING_DAYS * 2)).strftime("%Y-%m-%d")

    for c in candidates:
        code = str(c.get("code", "")).strip()
        name = str(c.get("name", "")).strip()

        if not code:
            continue

        result = YesterdayCandidateResult(code=code, name=name)

        ticker = _a_stock_ticker(code)
        try:
            raw = yf.download(ticker, start=start_date, end=end_date, progress=False, auto_adjust=True)
            df = _ensure_df(raw)
        except Exception as exc:
            result.error = f"数据下载失败: {exc}"
            result.has_anomaly = True
            result.anomaly_reasons.append("数据下载失败")
            results.append(result)
            errors.append(f"{name}({code}): 数据下载失败")
            continue

        if df is None or df.empty:
            # Yahoo Finance has limited BSE coverage; surface a clearer hint
            # for those tickers so users don't mistake it for a code bug.
            if ticker.endswith(".BJ"):
                result.error = "无交易数据（Yahoo Finance 不提供北交所历史数据）"
            else:
                result.error = "无交易数据"
            result.has_anomaly = True
            result.anomaly_reasons.append("无交易数据")
            results.append(result)
            continue

        if len(df) < 2:
            result.error = f"数据不足（仅 {len(df)} 个交易日，需要至少 2 个）"
            result.has_anomaly = True
            result.anomaly_reasons.append("数据不足")
            results.append(result)
            continue

        try:
            yesterday_idx = len(df) - 2
            today_idx = len(df) - 1

            # Dates
            yday_dt = df.iloc[yesterday_idx]["date"]
            today_dt = df.iloc[today_idx]["date"]
            if hasattr(yday_dt, "strftime"):
                result.signal_date = yday_dt.strftime("%Y-%m-%d")
                result.today_date = today_dt.strftime("%Y-%m-%d")
            else:
                result.signal_date = str(yday_dt)[:10]
                result.today_date = str(today_dt)[:10]

            if not signal_date_default:
                signal_date_default = result.signal_date
                today_date_default = result.today_date

            # Prices
            yesterday_open = float(df.iloc[yesterday_idx]["open"])
            yesterday_close = float(df.iloc[yesterday_idx]["close"])
            result.signal_price = _round(yesterday_close)

            result.today_open = _round(float(df.iloc[today_idx]["open"]))
            result.today_close = _round(float(df.iloc[today_idx]["close"]))
            result.today_high = _round(float(df.iloc[today_idx]["high"]))
            result.today_low = _round(float(df.iloc[today_idx]["low"]))

            # Returns
            result.today_return_pct = _round((result.today_close / yesterday_open - 1) * 100)
            result.today_high_return_pct = _round((result.today_high / yesterday_open - 1) * 100)
            result.today_low_return_pct = _round((result.today_low / yesterday_open - 1) * 100)

            # Pattern at yesterday
            result.pattern = _compute_pattern(df, yesterday_idx)

            # Volume ratio
            result.volume_ratio = 0
            if yesterday_idx >= 1:
                try:
                    yday_vol = float(df.iloc[yesterday_idx]["volume"])
                    prev_vol = float(df.iloc[yesterday_idx - 1]["volume"])
                    result.volume_ratio = _round(yday_vol / prev_vol) if prev_vol > 0 else 0
                except Exception:
                    pass

            # ---- 技术面分析（StockTrendAnalyzer）----
            try:
                analyzer = StockTrendAnalyzer()
                trend_result = analyzer.analyze(df.copy(), code)
                result.trend = TrendSnapshot(
                    trend_status=trend_result.trend_status.value if hasattr(trend_result.trend_status, 'value') else str(trend_result.trend_status),
                    trend_strength=_round(trend_result.trend_strength),
                    ma_alignment=trend_result.ma_alignment,
                    ma5=_round(trend_result.ma5),
                    ma10=_round(trend_result.ma10),
                    ma20=_round(trend_result.ma20),
                    ma60=_round(trend_result.ma60),
                    bias_ma5=_round(trend_result.bias_ma5),
                    volume_status=trend_result.volume_status.value if hasattr(trend_result.volume_status, 'value') else str(trend_result.volume_status),
                    volume_ratio_5d=_round(trend_result.volume_ratio_5d),
                    macd_dif=_round(trend_result.macd_dif, 4),
                    macd_dea=_round(trend_result.macd_dea, 4),
                    macd_bar=_round(trend_result.macd_bar, 4),
                    macd_status=trend_result.macd_status.value if hasattr(trend_result.macd_status, 'value') else str(trend_result.macd_status),
                    macd_signal=trend_result.macd_signal,
                    rsi_6=_round(trend_result.rsi_6),
                    rsi_12=_round(trend_result.rsi_12),
                    rsi_24=_round(trend_result.rsi_24),
                    rsi_status=trend_result.rsi_status.value if hasattr(trend_result.rsi_status, 'value') else str(trend_result.rsi_status),
                    buy_signal=trend_result.buy_signal.value if hasattr(trend_result.buy_signal, 'value') else str(trend_result.buy_signal),
                    signal_score=trend_result.signal_score,
                )
            except Exception as trend_err:
                logger.warning(f"Trend analysis failed for {code}: {trend_err}")

            # ---- Anomaly detection ----
            anomalies: list[str] = []
            if result.today_return_pct < -5:
                anomalies.append(f"今日大幅下跌 {result.today_return_pct:+.2f}%")
            elif result.today_return_pct < -2:
                anomalies.append(f"今日小幅亏损 {result.today_return_pct:+.2f}%")
            if result.volume_ratio > 3:
                anomalies.append(f"昨日放量异常 {result.volume_ratio:.1f}x")

            if anomalies:
                result.has_anomaly = True
                result.anomaly_reasons = anomalies

        except Exception as exc:
            result.error = f"回测计算失败: {exc}"
            result.has_anomaly = True
            result.anomaly_reasons.append("计算异常")

        results.append(result)

    # Summary
    total = len(results)
    anomaly_count = sum(1 for r in results if r.has_anomaly)
    valid_returns = [r.today_return_pct for r in results if r.error is None]
    avg_return = _round(sum(valid_returns) / max(len(valid_returns), 1))

    return YesterdayBacktestResponse(
        candidates=results,
        total=total,
        anomaly_count=anomaly_count,
        avg_return_pct=avg_return,
        signal_date=signal_date_default,
        today_date=today_date_default,
        errors=errors,
    )
