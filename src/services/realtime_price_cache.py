"""Realtime intraday price series cache.

Records per-stock intraday prices sampled from the 1-minute K-line feed. It is
the price counterpart of :mod:`src.services.realtime_volume_cache`: that one
tracks *volume* acceleration, this one tracks *price* acceleration so a rule
can fire on a short intraday surge (e.g. 49.07 -> 49.54 within two minutes)
even when volume stays flat.

Design notes:

- In-memory only (single process), same as the volume cache. The alert worker
  and the API share the process, so no cross-process store is needed.
- Snapshots are deduplicated per minute bucket: the latest sample wins, which
  keeps the series monotonic in time.
- Retention is generous (a full A-share session = 240 minutes) so a window of
  a few minutes always has enough points.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

import pandas as pd

# A 股每日交易时段：09:30-11:30 + 13:00-15:00 = 240 分钟
_TOTAL_A_SHARE_TRADING_MINUTES = 240

_PRICE_COLUMNS = ("close", "price", "close_price", "last")

# 每次喂入最多重放最近多少根 1 分钟 K 线（30 根 = 30 分钟，远大于分钟级窗口）
_REPLAY_BARS = 30


def _norm_code(stock_code: str) -> str:
    """Normalize a stock code for cache keying (lazy import avoids a circular
    dependency on ``data_provider.base``)."""
    try:
        from data_provider.base import normalize_stock_code

        return normalize_stock_code(stock_code)
    except Exception:
        return str(stock_code)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class RealtimePriceCache:
    """Thread-safe sliding-window cache of ``(timestamp, price)`` per stock."""

    def __init__(self, *, retention_minutes: int = _TOTAL_A_SHARE_TRADING_MINUTES) -> None:
        if retention_minutes <= 0:
            raise ValueError("retention_minutes must be positive")
        self._retention_seconds = retention_minutes * 60
        self._series: Dict[str, Deque[Tuple[float, float]]] = defaultdict(deque)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ writes
    def record(
        self,
        stock_code: str,
        price: Optional[float],
        *,
        at: Optional[datetime] = None,
    ) -> None:
        """Append a price snapshot. ``None`` / non-finite values are ignored."""
        stock_code = _norm_code(stock_code)
        if price is None:
            return
        try:
            value = float(price)
        except (TypeError, ValueError):
            return
        if value != value or value in (float("inf"), float("-inf")) or value <= 0:
            return
        ts = (at or _now_utc()).timestamp()
        with self._lock:
            bucket = self._series[stock_code]
            if bucket and int(ts // 60) == int(bucket[-1][0] // 60):
                bucket[-1] = (ts, value)
            else:
                bucket.append((ts, value))
            self._prune_locked(bucket, cutoff=ts - self._retention_seconds)

    def record_1min_kline(
        self,
        stock_code: str,
        *,
        bars: "pd.DataFrame",
        at: Optional[datetime] = None,
    ) -> Optional[float]:
        """Record every bar's close price using the bar's own timestamp.

        Replaying the whole frame (instead of only the last bar) backfills the
        minutes missed between two refresh cycles, so short windows always have
        real 1-minute resolution. Returns the latest recorded price.
        """
        if bars is None or len(bars) == 0:
            return None
        price_col = next((c for c in _PRICE_COLUMNS if c in bars.columns), None)
        if price_col is None:
            return None
        # 只取最近若干根：1 分钟 K 的 time 列是北京时间，直接按 UTC 解析会整体偏
        # 8 小时导致窗口过滤失效、混入跨时段旧价。这里改为以"当前时间 = 最后一根"
        # 向前倒推 1 分钟，既避开时区坑，也足够覆盖分钟级窗口。
        tail = bars.tail(_REPLAY_BARS)
        prices = pd.to_numeric(tail[price_col], errors="coerce").dropna()
        if prices.empty:
            return None
        now_ts = (at or _now_utc()).timestamp()
        n = len(prices)
        last_price = None
        for i in range(n):
            ts = now_ts - (n - 1 - i) * 60
            self.record(
                stock_code,
                float(prices.iloc[i]),
                at=datetime.fromtimestamp(ts, tz=timezone.utc),
            )
            last_price = float(prices.iloc[i])
        return last_price

    @staticmethod
    def _parse_bar_timestamps(bars: "pd.DataFrame", *, at: Optional[datetime], n: int) -> List[float]:
        """Resolve a real timestamp per bar, falling back to 1-minute spacing."""
        now = (at if at is not None else _now_utc()).timestamp()
        time_col = "time" if "time" in bars.columns else ("datetime" if "datetime" in bars.columns else None)
        if time_col is not None:
            try:
                parsed = pd.to_datetime(bars[time_col], errors="coerce")
                if parsed.notna().any():
                    base = datetime.fromtimestamp(now, tz=timezone.utc)
                    out: List[float] = []
                    for p in parsed:
                        if pd.isna(p):
                            out.append(now)
                            continue
                        out.append(
                            datetime(
                                base.year,
                                base.month,
                                base.day,
                                p.hour,
                                p.minute,
                                p.second,
                                tzinfo=timezone.utc,
                            ).timestamp()
                        )
                    while len(out) < n:
                        out.append(now)
                    return out[:n]
            except Exception:
                pass
        return [now - (n - 1 - i) * 60 for i in range(n)]

    # ------------------------------------------------------------------- reads
    def snapshot(
        self,
        stock_code: str,
        *,
        window_minutes: int = 3,
        now: Optional[datetime] = None,
    ) -> List[Tuple[datetime, float]]:
        """Return ``(timestamp, price)`` points within the window, oldest first."""
        stock_code = _norm_code(stock_code)
        if window_minutes <= 0:
            return []
        now_ts = (now or _now_utc()).timestamp()
        cutoff = now_ts - window_minutes * 60
        with self._lock:
            bucket = self._series.get(stock_code)
            if not bucket:
                return []
            filtered = [(ts, price) for ts, price in bucket if ts >= cutoff]
            filtered.sort(key=lambda pair: pair[0])
            return [(datetime.fromtimestamp(ts, tz=timezone.utc), price) for ts, price in filtered]

    def change_pct(
        self,
        stock_code: str,
        *,
        window_minutes: int = 3,
        now: Optional[datetime] = None,
    ) -> Optional[float]:
        """Percentage price change across the window (positive = rising).

        Uses the window's first and last sample, which matches how a trader
        eyeballs an intraday surge ("49.07 -> 49.54 in two minutes").
        """
        points = self.snapshot(stock_code, window_minutes=window_minutes, now=now)
        if len(points) < 2:
            return None
        start_price = points[0][1]
        end_price = points[-1][1]
        if start_price <= 0:
            return None
        return (end_price - start_price) / start_price * 100.0

    def size(self, stock_code: Optional[str] = None) -> int:
        with self._lock:
            if stock_code is None:
                return sum(len(b) for b in self._series.values())
            bucket = self._series.get(_norm_code(stock_code))
            return len(bucket) if bucket else 0

    def prune(self, *, now: Optional[datetime] = None) -> int:
        now_ts = (now or _now_utc()).timestamp()
        cutoff = now_ts - self._retention_seconds
        removed = 0
        with self._lock:
            for bucket in self._series.values():
                before = len(bucket)
                self._prune_locked(bucket, cutoff=cutoff)
                removed += before - len(bucket)
        return removed

    # ------------------------------------------------------------------ utils
    def _prune_locked(self, bucket: Deque[Tuple[float, float]], *, cutoff: float) -> None:
        while bucket and bucket[0][0] < cutoff:
            bucket.popleft()


_default_cache: Optional[RealtimePriceCache] = None
_default_lock = threading.Lock()


def get_default_cache() -> RealtimePriceCache:
    """Return the process-wide cache, creating it on first access."""
    global _default_cache
    if _default_cache is None:
        with _default_lock:
            if _default_cache is None:
                _default_cache = RealtimePriceCache()
    return _default_cache


def reset_default_cache() -> None:
    """Drop the singleton (tests only)."""
    global _default_cache
    with _default_lock:
        _default_cache = None


__all__ = ["RealtimePriceCache", "get_default_cache", "reset_default_cache"]
