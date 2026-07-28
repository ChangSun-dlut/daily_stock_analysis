# -*- coding: utf-8 -*-
"""
HiThink (同花顺) Financial-API data source adapter.

Provides A-share realtime quotes & daily K-line via the official HiThink
Financial-API (https://github.com/HiThink-Tech/Financial-API /
https://fuyao.aicubes.cn).

Design:
- Only serves A-shares (SH/SZ). Non-A-share symbols return None immediately.
- Two independent circuit breakers: one for realtime (snapshot), one for daily
  (historical). A dead realtime path does not affect daily and vice versa.
- Priority: 1 (high, but after TickFlow which typically carries richer data).

Config:
  HITHINK_FINANCE_API_KEY     —  API Key from fuyao.aicubes.cn
  HITHINK_PRIORITY            —  override default priority (optional, default 1)
"""

from __future__ import annotations

import logging
import os
import time as _time
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, normalize_stock_code
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_int

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API constants
# ---------------------------------------------------------------------------
_HITHINK_BASE_URL = "https://fuyao.aicubes.cn"
_SNAPSHOT_URL = f"{_HITHINK_BASE_URL}/api/a-share/prices/snapshot"
_HISTORICAL_URL = f"{_HITHINK_BASE_URL}/api/a-share/prices/historical"
_REQUEST_TIMEOUT = 15.0  # seconds for each HTTP call
_DAILY_DEAD_COOLDOWN = 300.0  # seconds — same as DataFetcherManager’s _daily_source_health

# --- code → thscode helpers -------------------------------------------------

_SH_PREFIXES = ("60", "68")   # 上海主板 + 科创板
_SZ_PREFIXES = ("00", "30")   # 深圳主板 + 创业板


def _to_thscode(stock_code: str) -> Optional[str]:
    """Convert a normalised 6-digit A-share code to a HiThink thscode.

    Returns ``None`` for non-A-share symbols (HK / US / indices etc.).
    """
    code = normalize_stock_code(stock_code)
    if len(code) != 6:
        return None
    if code.startswith(_SH_PREFIXES):
        return f"{code}.SH"
    if code.startswith(_SZ_PREFIXES):
        return f"{code}.SZ"
    # B-shares, indices, funds, convertible bonds — HiThink snapshot
    # supports them too but the mapping is more nuanced.  For now only
    # the most common A-share ranges are served.
    return None


def _date_to_ms(date_str: str) -> int:
    """Convert YYYY-MM-DD or YYYYMMDD to millisecond Unix timestamp (local time)."""
    import time as _time

    date_str = str(date_str).strip()
    if len(date_str) == 8 and date_str.isdigit():
        dt = datetime.strptime(date_str, "%Y%m%d")
    else:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    return int(_time.mktime(dt.timetuple())) * 1000


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class HithinkFetcher(BaseFetcher):
    """同花顺 Financial-API 实时行情数据源（仅 A 股）。"""

    name = "HithinkFetcher"
    priority = int(os.getenv("HITHINK_PRIORITY", "1"))

    def __init__(self, api_key: str = "") -> None:
        super().__init__()
        self._api_key = (api_key or os.getenv("HITHINK_FINANCE_API_KEY", "")).strip()
        self._dead = False                # circuit breaker — realtime snapshot path
        self._daily_dead_until = 0.0      # cooldown timestamp — daily historical path

    # ------------------------------------------------------------------
    # Daily circuit breaker helpers
    # ------------------------------------------------------------------

    @property
    def _daily_dead(self) -> bool:
        """Whether the daily (historical) path is currently dead.

        After a failure the path is dead for ``_DAILY_DEAD_COOLDOWN``
        seconds, then the breaker auto-resets so subsequent requests
        can retry the endpoint.
        """
        if self._daily_dead_until <= 0:
            return False
        if _time.time() < self._daily_dead_until:
            return True
        # Cooldown expired — auto-reset
        self._daily_dead_until = 0.0
        logger.info("[%s] 日线熔断冷却已过期，自动恢复", self.name)
        return False

    @_daily_dead.setter
    def _daily_dead(self, value: bool) -> None:
        if value:
            self._daily_dead_until = _time.time() + _DAILY_DEAD_COOLDOWN
            logger.warning(
                "[%s] 日线接口已熔断，将在 %.0f 秒后自动恢复",
                self.name, _DAILY_DEAD_COOLDOWN,
            )
        else:
            self._daily_dead_until = 0.0

    # ------------------------------------------------------------------
    # Daily K-line: _fetch_raw_data / _normalize_data
    # ------------------------------------------------------------------

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch A-share daily K-line from HiThink historical endpoint.

        Called by ``BaseFetcher.get_daily_data()``.
        """
        if self._daily_dead:
            raise DataFetchError(
                f"[{self.name}] 日线接口已熔断，跳过 {stock_code}"
            )
        if not self._api_key:
            raise DataFetchError(
                f"[{self.name}] API Key 未配置，无法获取日线 {stock_code}"
            )

        thscode = _to_thscode(stock_code)
        if thscode is None:
            raise DataFetchError(
                f"[{self.name}] {stock_code} 不是 A 股标的，跳过日线"
            )

        # Convert YYYY-MM-DD / YYYYMMDD to millisecond Unix timestamps
        start_ms = _date_to_ms(start_date)
        end_ms = _date_to_ms(end_date)

        try:
            resp = requests.get(
                _HISTORICAL_URL,
                params={
                    "thscode": thscode,
                    "interval": "1d",
                    "start": start_ms,
                    "end": end_ms,
                    "adjust": "forward",
                },
                headers={"X-api-key": self._api_key},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            self._daily_dead = True
            raise DataFetchError(
                f"[{self.name}] 日线 API 调用失败 {thscode}: {exc}，已熔断"
            ) from exc

        if payload.get("code") != 0:
            self._daily_dead = True
            raise DataFetchError(
                f"[{self.name}] 日线 API 返回错误 code={payload.get('code')} "
                f"msg={payload.get('message')}，已熔断"
            )

        data = payload.get("data") or {}
        items = data.get("item") or []

        if not items:
            # Empty result is not a fatal error — the stock may not have
            # data for the requested range (e.g. newly listed).
            return pd.DataFrame()

        return pd.DataFrame(items)

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """Map HiThink historical field names to STANDARD_COLUMNS.

        HiThink → standard:
          date_ms      → date          (ms → datetime)
          open_price   → open
          high_price   → high
          low_price    → low
          close_price  → close
          volume       → volume        (shares, kept as-is)
          turnover     → amount        (CNY)
        """
        if df is None or df.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        normalized = pd.DataFrame(index=df.index)
        normalized["stock_code"] = normalize_stock_code(stock_code)

        # date
        if "date_ms" in df.columns:
            normalized["date"] = pd.to_datetime(
                df["date_ms"], unit="ms"
            ).dt.normalize()

        # OHLCV
        _col_map = {
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
            "close_price": "close",
            "volume": "volume",
            "turnover": "amount",
        }
        for src, dst in _col_map.items():
            if src in df.columns:
                normalized[dst] = pd.to_numeric(df[src], errors="coerce")

        # pct_chg: computed day-over-day from close.  _calculate_indicators in
        # BaseFetcher does NOT compute pct_chg, so we do it here.
        if "close" in normalized.columns:
            prev = normalized["close"].shift(1)
            normalized["pct_chg"] = (
                (normalized["close"] - prev) / prev.replace(0, pd.NA) * 100
            ).round(2)
            normalized["pct_chg"] = normalized["pct_chg"].fillna(0.0)

        return normalized

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def is_available_for_request(self, capability: str = "") -> bool:
        """Check whether the fetcher can serve a request.

        *realtime_quote* → checks ``_dead``
        *daily_data* → checks ``_daily_dead``
        Without a capability hint we fall back to ``_dead or _daily_dead``.
        """
        if not self._api_key:
            return False
        if capability == "realtime_quote":
            return not self._dead
        if capability == "daily_data":
            return not self._daily_dead
        return not self._dead and not self._daily_dead

    # ------------------------------------------------------------------
    # Core: realtime quote
    # ------------------------------------------------------------------

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """Fetch a single A-share realtime snapshot from HiThink."""

        if self._dead:
            return None
        if not self._api_key:
            logger.debug("HithinkFetcher: API Key 未配置，跳过")
            return None

        thscode = _to_thscode(stock_code)
        if thscode is None:
            # Not an A-share — let another fetcher handle it
            return None

        normalised = normalize_stock_code(stock_code)

        try:
            resp = requests.get(
                _SNAPSHOT_URL,
                params={"thscodes": thscode},
                headers={"X-api-key": self._api_key},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            self._dead = True
            logger.info(
                "HithinkFetcher: API 调用失败 %s: %s，已熔断后续请求",
                thscode, exc,
            )
            return None

        # HiThink wraps responses in a standard envelope
        if payload.get("code") != 0:
            self._dead = True
            logger.info(
                "HithinkFetcher: API 返回错误 code=%s msg=%s，已熔断后续请求",
                payload.get("code"), payload.get("message"),
            )
            return None

        data = payload.get("data") or {}
        items = data.get("item") or []

        if not items:
            logger.debug("HithinkFetcher: %s 无行情数据", thscode)
            return None

        item = items[0]

        # Required field — if missing the quote is unusable
        price = item.get("last_price")
        if price is None:
            logger.debug("HithinkFetcher: %s 缺少 last_price", thscode)
            return None

        return UnifiedRealtimeQuote(
            code=normalised,
            name="",                               # snapshot does not carry name
            source=RealtimeSource.HITHINK,
            price=float(price),
            change_pct=_safe_float(item.get("price_change_ratio_pct")),
            change_amount=_safe_float(item.get("price_change")),
            volume=_safe_int(item.get("volume")),
            amount=_safe_float(item.get("turnover")),
            high=_safe_float(item.get("high_price")),
            low=_safe_float(item.get("low_price")),
            open_price=_safe_float(item.get("open_price")),
            pre_close=_safe_float(item.get("prev_price")),
        )


# ---------------------------------------------------------------------------
# tiny local helpers (avoid importing the whole realtime_types surface)
# ---------------------------------------------------------------------------

def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
