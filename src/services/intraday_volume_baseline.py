# -*- coding: utf-8 -*-
"""盘中「同比昨日同期成交量」基线计算。

服务于 ``volume_yoy_surge_rt`` 分钟级告警：判断**今日开盘至今的累计成交量**
相对**昨日同一时间段的累计成交量**放大了多少倍。

计算口径::

    yoy_volume_ratio = 今日累计成交量 / 昨日同期累计成交量

两部分数据来源：

- **今日累计成交量**：实时行情的 ``volume`` 字段（当日累计成交量），由
  ``DataFetcherManager.get_realtime_quote`` 提供。
- **昨日同期累计成交量**（基线）：优先用上一交易日的**分钟 K 线**按同一进度
  精确截断；分钟源不可用时，退化为「昨日全天成交量 × 经验日内分布曲线」折算。

为什么需要经验分布曲线：A 股日内成交量呈 U 型（早盘、尾盘高，午后低），
简单的线性折算（``elapsed / 240``）会系统性**低估**昨日早盘基线，导致早盘
时段永假阳性。曲线见 :data:`_INTRADAY_VOLUME_SHARE_5MIN`（单位：占全天 %）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

try:  # pragma: no cover - tzdata may be missing in some environments
    from zoneinfo import ZoneInfo

    _SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - defensive
    _SHANGHAI_TZ = None


# ---------------------------------------------------------------------------
# A 股典型日内成交量分布：每 5 分钟占全天成交量的百分比（开盘 09:30 起，48 个桶）。
#
# 取自 A 股日内成交的通用经验形态：开盘半小时集中释放（约 22%），随后逐级回落，
# 午后再探底，尾盘 10 分钟（含集合竞价）回升。合计不要求严格 100%，
# :func:`cumulative_intraday_share` 会按需归一化，因此只需保持相对形状正确。
# ---------------------------------------------------------------------------
_INTRADAY_VOLUME_SHARE_5MIN: Tuple[float, ...] = (
    # 09:30 - 10:00（早盘释放）：合计约 18.5%，归一化后开盘半小时约占全天 22%，
    #         与 A 股「早盘半小时占全天两成左右」的经验区间一致
    5.0, 3.3, 2.9, 2.65, 2.45, 2.25,
    # 10:00 - 10:30
    2.5, 2.35, 2.2, 2.1, 2.0, 1.95,
    # 10:30 - 11:00
    1.9, 1.8, 1.75, 1.7, 1.65, 1.6,
    # 11:00 - 11:30
    1.6, 1.5, 1.45, 1.4, 1.35, 1.35,
    # 13:00 - 13:30（午后重启）
    1.9, 1.7, 1.6, 1.5, 1.45, 1.4,
    # 13:30 - 14:00
    1.35, 1.3, 1.25, 1.2, 1.2, 1.15,
    # 14:00 - 14:30（午后低点）
    1.15, 1.1, 1.1, 1.1, 1.1, 1.15,
    # 14:30 - 15:00（尾盘回升 + 集合竞价）
    1.2, 1.25, 1.35, 1.5, 1.7, 3.0,
)

_BUCKET_MINUTES = 5
_TOTAL_BUCKETS = len(_INTRADAY_VOLUME_SHARE_5MIN)  # 48 × 5min = 240min
_SHARE_TOTAL = float(sum(_INTRADAY_VOLUME_SHARE_5MIN))

# 距低保：避免 elapsed 极小（开盘瞬间）时基线趋近 0 导致倍数异常放大。
_MIN_BASELINE_SHARE = 0.005


def _shanghai_now() -> datetime:
    if _SHANGHAI_TZ is not None:
        return datetime.now(_SHANGHAI_TZ)
    return datetime.now()


def _session_key(now: Optional[datetime] = None) -> str:
    """当日沪深交易日字符串（YYYYMMDD），用作昨日量缓存的版本键。"""
    return _shanghai_now().strftime("%Y%m%d") if now is None else now.strftime("%Y%m%d")


def cumulative_intraday_share(elapsed_minutes: float) -> float:
    """折算：开盘至 ``elapsed_minutes`` 分钟为止累计成交量占全天的比例（0~1）。

    ``elapsed_minutes`` 取值范围 0~240（A 股一个交易日的总交易分钟数）。
    桶内按线性插值，保证相邻分钟之间是连续变化的。
    """
    try:
        elapsed = float(elapsed_minutes)
    except (TypeError, ValueError):
        return 0.0
    if elapsed <= 0 or _SHARE_TOTAL <= 0:
        return 0.0

    # 超出全天时钳制为 1（例如收盘后仍被调用）。
    if elapsed >= _TOTAL_BUCKETS * _BUCKET_MINUTES:
        return 1.0

    full_buckets = int(elapsed // _BUCKET_MINUTES)
    partial_ratio = (elapsed - full_buckets * _BUCKET_MINUTES) / float(_BUCKET_MINUTES)

    accumulated = float(sum(_INTRADAY_VOLUME_SHARE_5MIN[:full_buckets]))
    if full_buckets < _TOTAL_BUCKETS:
        accumulated += _INTRADAY_VOLUME_SHARE_5MIN[full_buckets] * partial_ratio

    share = accumulated / _SHARE_TOTAL
    return max(_MIN_BASELINE_SHARE, min(1.0, share))


# ---------------------------------------------------------------------------
# 上一交易日全天成交量（带进程内缓存）
# ---------------------------------------------------------------------------
_prev_session_cache: Dict[str, Tuple[Optional[float], float]] = {}
_prev_session_lock = threading.Lock()
# 上一交易日的量当天不再变化，TTL 给足（30 分钟）避免重复打日 K 接口。
_PREV_SESSION_TTL_SECONDS = 1800


def previous_session_volume(stock_code: str, *, lookback_days: int = 10) -> Optional[float]:
    """返回上一交易日的全天成交量（股）。失败返回 ``None``。

    盘中调用时 ``get_daily_data`` 返回的最后一行可能是**当日**的未收盘数据，
    因此当最后一行日期 == 今天时取倒数第二行。
    """
    if not stock_code:
        return None

    cache_key = f"{stock_code}|{_session_key()}"
    now_epoch = time.time()
    with _prev_session_lock:
        cached = _prev_session_cache.get(cache_key)
        if cached is not None and cached[1] > now_epoch:
            return cached[0]

    value = _fetch_previous_session_volume(stock_code, lookback_days=lookback_days)
    with _prev_session_lock:
        _prev_session_cache[cache_key] = (value, now_epoch + _PREV_SESSION_TTL_SECONDS)
    return value


def _fetch_previous_session_volume(stock_code: str, *, lookback_days: int = 10) -> Optional[float]:
    try:
        from data_provider.base import DataFetcherManager

        manager = DataFetcherManager()
        df, _source = manager.get_daily_data(stock_code, days=max(6, lookback_days))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[同比放量] %s 取日 K 失败: %s", stock_code, exc)
        return None

    if df is None or getattr(df, "empty", True):
        return None

    vol_col = "volume" if "volume" in df.columns else ("vol" if "vol" in df.columns else None)
    if vol_col is None or "date" not in df.columns:
        return None

    try:
        dates = [str(d)[:10] for d in df["date"]]
        volumes = [float(v) for v in df[vol_col]]
    except Exception:  # pragma: no cover - defensive
        return None
    if not dates:
        return None

    today = _shanghai_now().strftime("%Y-%m-%d")
    # 若最新一行是当天（盘中未收盘数据），回退一行取上一交易日。
    index = -1 if dates[-1] != today else (-2 if len(dates) >= 2 else -1)
    try:
        volume = volumes[index]
    except IndexError:
        return None
    if volume <= 0:
        return None
    return volume


def previous_session_same_period_volume(
    stock_code: str,
    elapsed_minutes: float,
) -> Optional[float]:
    """昨日同一时间段的累计成交量（基线）。

    = 上一交易日全天成交量 × ``cumulative_intraday_share(elapsed_minutes)``。
    """
    total = previous_session_volume(stock_code)
    if not total:
        return None
    return total * cumulative_intraday_share(elapsed_minutes)


__all__ = [
    "cumulative_intraday_share",
    "previous_session_same_period_volume",
    "previous_session_volume",
]
