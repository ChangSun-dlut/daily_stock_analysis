"""Realtime volume-ratio time series cache.

Records per-stock ``volume_ratio`` snapshots as the realtime quote API is hit
(by the web UI's ``⚡`` refresh button, the backend's spot-quote enrichment, or
the alert worker). Each snapshot is timestamped and pruned automatically.

The cache is consumed by the ``volume_spike_rt`` alert indicator, which detects
*accelerating* volume-ratio (positive slope over a sliding window) rather than
a static absolute-value threshold.

Design notes:

- In-memory only (single-process). The alert worker and the API live in the
  same process; cross-process persistence would require Redis/SQLite.
- Snapshot deduplication: if the same ``(stock_code, minute_bucket)`` is
  recorded twice, the latest value wins.
- Trade session is detected via ``Asia/Shanghai`` so the alert only triggers
  during market hours even if stale snapshots linger across sessions.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, Iterable, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# A 股每日交易时段：09:30-11:30 + 13:00-15:00 = 240 分钟
_TOTAL_A_SHARE_TRADING_MINUTES = 240


def _shanghai_now() -> datetime:
    """Return current UTC datetime; downstream code uses UTC for monotonic time."""
    return datetime.now(timezone.utc)


def _norm_code(stock_code: str) -> str:
    """Normalize a stock code for cache keying (lazy import avoids a circular
    dependency on ``data_provider.base``). Falls back to the raw code if the
    normalizer is unavailable. This keeps records and reads consistent with the
    system-wide convention (``DataFetcherManager`` / alert evaluator normalize
    codes), so e.g. ``600519.SH`` and ``600519`` resolve to the same bucket.
    """
    try:
        from data_provider.base import normalize_stock_code
        return normalize_stock_code(stock_code)
    except Exception:
        return stock_code


class RealtimeVolumeCache:
    """Thread-safe sliding-window cache of ``(timestamp, volume_ratio)`` per stock.

    Parameters
    ----------
    retention_minutes:
        How long a snapshot is kept. Anything older is pruned on each
        :meth:`record` / :meth:`prune` call. Default 60 minutes — long enough
        for a 30-min slope window with margin, short enough that overnight
        snapshots don't leak across sessions.
    """

    def __init__(self, *, retention_minutes: int = 60) -> None:
        if retention_minutes <= 0:
            raise ValueError("retention_minutes must be positive")
        self._retention_seconds = retention_minutes * 60
        # stock_code -> sorted deque of (utc_epoch_seconds, volume_ratio)
        self._series: Dict[str, Deque[Tuple[float, float]]] = defaultdict(deque)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ writes
    def record(
        self,
        stock_code: str,
        volume_ratio: Optional[float],
        *,
        at: Optional[datetime] = None,
    ) -> None:
        """Append a snapshot. ``None`` or non-finite values are ignored."""
        stock_code = _norm_code(stock_code)
        if volume_ratio is None:
            return
        try:
            ratio = float(volume_ratio)
        except (TypeError, ValueError):
            return
        # Avoid NaN / inf corrupting the slope computation.
        if ratio != ratio or ratio in (float("inf"), float("-inf")):
            return
        ts = at if isinstance(at, (int, float)) else (at or _shanghai_now()).timestamp()
        with self._lock:
            bucket = self._series[stock_code]
            # Dedup by minute: if the previous point is in the same minute,
            # overwrite it so the slope reflects the freshest reading.
            if bucket and int(ts // 60) == int(bucket[-1][0] // 60):
                bucket[-1] = (ts, ratio)
            else:
                bucket.append((ts, ratio))
            self._prune_locked(bucket, cutoff=ts - self._retention_seconds)

    def record_many(
        self,
        items: Iterable[Tuple[str, Optional[float]]],
        *,
        at: Optional[datetime] = None,
    ) -> None:
        """Bulk record — used by the alert worker after a batch fetch."""
        ts = at if isinstance(at, (int, float)) else (at or _shanghai_now()).timestamp()
        with self._lock:
            for stock_code, ratio in items:
                stock_code = _norm_code(stock_code)
                if ratio is None:
                    continue
                try:
                    r = float(ratio)
                except (TypeError, ValueError):
                    continue
                if r != r or r in (float("inf"), float("-inf")):
                    continue
                bucket = self._series[stock_code]
                if bucket and int(ts // 60) == int(bucket[-1][0] // 60):
                    bucket[-1] = (ts, r)
                else:
                    bucket.append((ts, r))
                self._prune_locked(bucket, cutoff=ts - self._retention_seconds)

    def record_1min_kline(
        self,
        stock_code: str,
        *,
        bars: "pd.DataFrame",
        avg_daily_volume: float,
        elapsed_minutes: int,
        at: Optional[datetime] = None,
    ) -> Optional[float]:
        """用 1 分钟 K 线的相邻增量计算「分钟级量比」并写入缓存。

        与 spot-quote 自算量比（当日累计成交量 / (5日均量 × 已开盘分钟/240)）
        不同，这里用 **相邻两根 1 分钟 K 线的成交量增量** 得到「当前这一分钟」
        的当量比，能捕捉短线脉冲，从而让 ``volume_spike_rt`` 的斜率 / 峰值
        计算精确到分钟粒度。

        ``bars`` 为标准 1 分钟 K 线 DataFrame（含 ``volume`` 列）。
        ``avg_daily_volume`` 近 N 日日均成交量（股）。
        ``elapsed_minutes`` 当日已开盘分钟数（用于把单分钟量换算到全天当量）。
        返回当量比：本分钟增量成交量 / (日均成交量 / 240)。
        """
        if bars is None or len(bars) == 0 or avg_daily_volume is None or avg_daily_volume <= 0:
            return None
        if elapsed_minutes <= 0:
            return None
        vol_col = "volume" if "volume" in bars.columns else None
        if vol_col is None:
            return None
        volumes = pd.to_numeric(bars[vol_col], errors="coerce").dropna()
        if len(volumes) < 2:
            return None
        minute_expect = avg_daily_volume / float(_TOTAL_A_SHARE_TRADING_MINUTES)
        if minute_expect <= 0:
            return None

        # 遍历**全部** 1 分钟 K 线，对相邻两根求增量并换算成单分钟当量比，
        # 用每根分钟**真实时间戳**（从 bars 的 time 列解析；失败则按 at 倒推兜底）
        # 写入。这样即便刷新间隔 > 1 分钟，也能补齐这段时间缺失的分钟点，缓存里
        # 才是真正的 1 分钟粒度序列，slope/peak 才有意义。
        ts_list = self._parse_bar_timestamps(bars, at=at, n=len(volumes))
        last_ratio = None
        for i in range(1, len(volumes)):
            delta = max(0.0, float(volumes.iloc[i]) - float(volumes.iloc[i - 1]))
            ratio = delta / minute_expect
            if ratio != ratio or ratio in (float("inf"), float("-inf")):
                continue
            if ratio > 50:
                ratio = 50.0
            self.record(stock_code, ratio, at=ts_list[i])
            last_ratio = ratio
        return last_ratio

    @staticmethod
    def _parse_bar_timestamps(bars: "pd.DataFrame", *, at: Optional[datetime], n: int) -> List[float]:
        """为每根 1 分钟 K 线解析真实时间戳（用于 at）。

        优先用 bars 的 time 列（pytdx 返回 datetime/字符串）；解析失败则用基准
        时间 at 倒推 1 分钟间隔，保证相邻点按真实顺序分布、不被 dedup 合并。
        """
        now = (at if at is not None else _shanghai_now()).timestamp()
        time_col = "time" if "time" in bars.columns else ("datetime" if "datetime" in bars.columns else None)
        if time_col is not None:
            try:
                parsed = pd.to_datetime(bars[time_col], errors="coerce")
                if parsed.notna().any():
                    base = datetime.fromtimestamp(now, tz=timezone.utc)
                    out = []
                    for p in parsed:
                        if pd.isna(p):
                            out.append(now)
                            continue
                        dt = datetime(
                            base.year, base.month, base.day,
                            p.hour, p.minute, p.second,
                            tzinfo=timezone.utc,
                        )
                        out.append(dt.timestamp())
                    while len(out) < n:
                        out.append(now)
                    return out[:n]
            except Exception:
                pass
        # 兜底：基准时间向前倒推 1 分钟间隔
        return [now - (n - 1 - i) * 60 for i in range(n)]

    # ------------------------------------------------------------------ reads
    def snapshot(
        self,
        stock_code: str,
        *,
        window_minutes: int = 30,
        now: Optional[datetime] = None,
    ) -> List[Tuple[datetime, float]]:
        """Return points within the last ``window_minutes`` minutes (oldest first)."""
        stock_code = _norm_code(stock_code)
        if window_minutes <= 0:
            return []
        now_ts = (now or _shanghai_now()).timestamp()
        cutoff = now_ts - window_minutes * 60
        with self._lock:
            bucket = self._series.get(stock_code)
            if not bucket:
                return []
            # Sort by timestamp to defend against out-of-order inserts (the
            # minute-bucket dedup can leave the deque in slightly stale order
            # when callers pass explicit ``at`` timestamps).
            filtered = [(ts, ratio) for ts, ratio in bucket if ts >= cutoff]
            filtered.sort(key=lambda pair: pair[0])
            return [
                (datetime.fromtimestamp(ts, tz=timezone.utc), ratio)
                for ts, ratio in filtered
            ]

    def slope(
        self,
        stock_code: str,
        *,
        window_minutes: int = 30,
        now: Optional[datetime] = None,
    ) -> Optional[float]:
        """Linear-regression slope of ``volume_ratio`` vs minutes-since-baseline.

        Returns ``None`` if fewer than 2 points are available. The slope is
        expressed in ``volume_ratio per minute`` so a 30-minute window rising
        from 1.0 to 2.5 yields ``+0.05/min`` (``+0.25/5-min``)."""
        points = self.snapshot(stock_code, window_minutes=window_minutes, now=now)
        if len(points) < 2:
            return None
        base_ts = points[0][0].timestamp()
        xs: List[float] = [(p[0].timestamp() - base_ts) / 60.0 for p in points]
        ys: List[float] = [p[1] for p in points]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        den = sum((x - mean_x) ** 2 for x in xs)
        if den <= 0:
            return None
        return num / den

    def current_ratio(
        self,
        stock_code: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[float]:
        """Return the latest cached ratio, or ``None`` if empty/stale."""
        stock_code = _norm_code(stock_code)
        now_ts = (now or _shanghai_now()).timestamp()
        cutoff = now_ts - self._retention_seconds
        with self._lock:
            bucket = self._series.get(stock_code)
            if not bucket:
                return None
            ts, ratio = bucket[-1]
            if ts < cutoff:
                return None
            return ratio

    def peak_ratio(
        self,
        stock_code: str,
        *,
        window_minutes: int = 30,
        now: Optional[datetime] = None,
    ) -> Optional[float]:
        """Return the maximum cached ratio within the last ``window_minutes``.

        Useful for catching *short* volume spikes (e.g. a 3-minute burst) that
        are invisible to slope-over-window but show up as one high sample.
        """
        points = self.snapshot(stock_code, window_minutes=window_minutes, now=now)
        if not points:
            return None
        return max(ratio for _ts, ratio in points)

    def size(self, stock_code: Optional[str] = None) -> int:
        with self._lock:
            if stock_code is None:
                return sum(len(b) for b in self._series.values())
            stock_code = _norm_code(stock_code)
            bucket = self._series.get(stock_code)
            return len(bucket) if bucket else 0

    def prune(self, *, now: Optional[datetime] = None) -> int:
        """Drop expired snapshots across all stocks. Returns count removed."""
        now_ts = (now or _shanghai_now()).timestamp()
        cutoff = now_ts - self._retention_seconds
        removed = 0
        with self._lock:
            for bucket in self._series.values():
                before = len(bucket)
                self._prune_locked(bucket, cutoff=cutoff)
                removed += before - len(bucket)
        return removed

    # ------------------------------------------------------------------ utils
    def _prune_locked(
        self,
        bucket: Deque[Tuple[float, float]],
        *,
        cutoff: float,
    ) -> None:
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()


# Module-level singleton — most callers should use this rather than
# instantiating their own. Tests can pass a fresh instance.
_default_cache: Optional[RealtimeVolumeCache] = None
_default_lock = threading.Lock()


def get_default_cache() -> RealtimeVolumeCache:
    """Return the process-wide cache, creating it on first access."""
    global _default_cache
    if _default_cache is None:
        with _default_lock:
            if _default_cache is None:
                _default_cache = RealtimeVolumeCache()
    return _default_cache


def reset_default_cache() -> None:
    """Drop the singleton (tests only)."""
    global _default_cache
    with _default_lock:
        _default_cache = None


__all__ = [
    "RealtimeVolumeCache",
    "get_default_cache",
    "reset_default_cache",
]