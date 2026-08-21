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

logger = logging.getLogger(__name__)


def _shanghai_now() -> datetime:
    """Return current UTC datetime; downstream code uses UTC for monotonic time."""
    return datetime.now(timezone.utc)


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
        if volume_ratio is None:
            return
        try:
            ratio = float(volume_ratio)
        except (TypeError, ValueError):
            return
        # Avoid NaN / inf corrupting the slope computation.
        if ratio != ratio or ratio in (float("inf"), float("-inf")):
            return
        ts = (at or _shanghai_now()).timestamp()
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
        ts = (at or _shanghai_now()).timestamp()
        with self._lock:
            for stock_code, ratio in items:
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

    # ------------------------------------------------------------------ reads
    def snapshot(
        self,
        stock_code: str,
        *,
        window_minutes: int = 30,
        now: Optional[datetime] = None,
    ) -> List[Tuple[datetime, float]]:
        """Return points within the last ``window_minutes`` minutes (oldest first)."""
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

    def size(self, stock_code: Optional[str] = None) -> int:
        with self._lock:
            if stock_code is None:
                return sum(len(b) for b in self._series.values())
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