"""
盘中分时量比自算：核心单元测试
- _trading_minutes_elapsed 各时段语义
- _enrich_intraday_volume_ratio happy path / 跳过条件 / 缓存命中
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from data_provider.base import (
    DataFetcherManager,
    _SHANGHAI_TZ,
    _trading_minutes_elapsed,
)


# === _trading_minutes_elapsed ===

@pytest.mark.parametrize(
    "dt,expected,label",
    [
        (datetime(2026, 8, 21, 8, 0, tzinfo=_SHANGHAI_TZ), 0.0, "盘前 08:00"),
        (datetime(2026, 8, 21, 9, 30, tzinfo=_SHANGHAI_TZ), 0.0, "开盘瞬间 09:30"),
        (datetime(2026, 8, 21, 10, 30, tzinfo=_SHANGHAI_TZ), 60.0, "上午 10:30"),
        (datetime(2026, 8, 21, 11, 30, tzinfo=_SHANGHAI_TZ), 120.0, "上午结束 11:30"),
        (datetime(2026, 8, 21, 12, 30, tzinfo=_SHANGHAI_TZ), 120.0, "午休 12:30"),
        (datetime(2026, 8, 21, 13, 0, tzinfo=_SHANGHAI_TZ), 120.0, "下午开盘 13:00"),
        (datetime(2026, 8, 21, 14, 30, tzinfo=_SHANGHAI_TZ), 210.0, "下午 14:30"),
        (datetime(2026, 8, 21, 15, 0, tzinfo=_SHANGHAI_TZ), 240.0, "收盘 15:00"),
        (datetime(2026, 8, 21, 16, 0, tzinfo=_SHANGHAI_TZ), 240.0, "盘后 16:00"),
    ],
)
def test_trading_minutes_elapsed(dt, expected, label):
    actual = _trading_minutes_elapsed(now=dt)
    assert abs(actual - expected) < 0.01, f"{label} expected={expected} actual={actual}"


# === _enrich_intraday_volume_ratio ===

def _make_quote(**overrides):
    base = {
        "code": "002092",
        "name": "中泰化学",
        "volume": 589_058,
        "amount": 261_953_344,
        "market": "cn",
        "volume_ratio": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_config(**overrides):
    base = SimpleNamespace(
        enable_intraday_volume_ratio=True,
        intraday_volume_ratio_lookback_days=5,
        intraday_volume_ratio_cache_ttl=300,
    )
    base.__dict__.update(overrides)
    return base


class _FakeMgr:
    """只暴露 _enrich_intraday_volume_ratio 用到的接口。"""

    def __init__(self, df=None, raise_exc=False):
        self._df = df
        self._raise_exc = raise_exc
        self.daily_calls = 0
        DataFetcherManager._volume_ratio_5d_cache.clear()

    def _enrich_intraday_volume_ratio(self, quote):
        return DataFetcherManager._enrich_intraday_volume_ratio(self, quote)

    def _get_volume_ratio_5d_avg(self, code, lookback_days):
        self.daily_calls += 1
        if self._raise_exc:
            raise RuntimeError("mocked daily error")
        if self._df is None or self._df.empty:
            return None
        return float(self._df["volume"].tail(lookback_days).mean())


def test_enrich_computes_ratio_at_trading_time(monkeypatch):
    """上午 10:30 → 已开盘 60 分钟 / 240 = 25%；
    5 日均量 2000000，则期望 ratio = 589058 / (2000000 * 0.25) = 1.1771。
    """
    df = pd.DataFrame({"volume": [2_000_000] * 5})
    mgr = _FakeMgr(df=df)
    quote = _make_quote()
    fake_now = datetime(2026, 8, 21, 10, 30, tzinfo=_SHANGHAI_TZ)

    with patch("src.config.get_config", return_value=_fake_config()), \
         patch("data_provider.base._trading_minutes_elapsed", return_value=60.0):
        mgr._enrich_intraday_volume_ratio(quote)

    assert quote.volume_ratio == pytest.approx(1.1771, rel=1e-3)


def test_enrich_skips_pre_market(monkeypatch):
    """盘前不计算（已开盘 0 分钟会导致 ratio=∞）。"""
    df = pd.DataFrame({"volume": [2_000_000] * 5})
    mgr = _FakeMgr(df=df)
    quote = _make_quote()

    with patch("src.config.get_config", return_value=_fake_config()), \
         patch("data_provider.base._trading_minutes_elapsed", return_value=0.0):
        mgr._enrich_intraday_volume_ratio(quote)

    assert quote.volume_ratio is None
    assert mgr.daily_calls == 0


def test_enrich_skips_when_volume_zero(monkeypatch):
    """当前成交为 0 时跳过（避免 ratio=0 误判）。"""
    mgr = _FakeMgr(df=pd.DataFrame({"volume": [2_000_000] * 5}))
    quote = _make_quote(volume=0)

    with patch("src.config.get_config", return_value=_fake_config()), \
         patch("data_provider.base._trading_minutes_elapsed", return_value=60.0):
        mgr._enrich_intraday_volume_ratio(quote)

    assert quote.volume_ratio is None
    assert mgr.daily_calls == 0


def test_enrich_skips_when_feature_disabled(monkeypatch):
    mgr = _FakeMgr(df=pd.DataFrame({"volume": [2_000_000] * 5}))
    quote = _make_quote()

    with patch("src.config.get_config", return_value=_fake_config(enable_intraday_volume_ratio=False)), \
         patch("data_provider.base._trading_minutes_elapsed", return_value=60.0):
        mgr._enrich_intraday_volume_ratio(quote)

    assert quote.volume_ratio is None
    assert mgr.daily_calls == 0


def test_enrich_skips_non_a_share(monkeypatch):
    mgr = _FakeMgr(df=pd.DataFrame({"volume": [2_000_000] * 5}))
    quote = _make_quote(market="hk")

    with patch("src.config.get_config", return_value=_fake_config()), \
         patch("data_provider.base._trading_minutes_elapsed", return_value=60.0):
        mgr._enrich_intraday_volume_ratio(quote)

    assert quote.volume_ratio is None
    assert mgr.daily_calls == 0


def test_enrich_preserves_existing_ratio(monkeypatch):
    """上游已给出 volume_ratio 时不覆盖。"""
    mgr = _FakeMgr(df=pd.DataFrame({"volume": [2_000_000] * 5}))
    quote = _make_quote(volume_ratio=3.14)

    with patch("src.config.get_config", return_value=_fake_config()), \
         patch("data_provider.base._trading_minutes_elapsed", return_value=60.0):
        mgr._enrich_intraday_volume_ratio(quote)

    assert quote.volume_ratio == 3.14
    assert mgr.daily_calls == 0


def test_enrich_daily_failure_is_silent(monkeypatch):
    """日 K 拉取失败不应影响主流程，volume_ratio 保持 None。"""
    mgr = _FakeMgr(raise_exc=True)
    quote = _make_quote()

    with patch("src.config.get_config", return_value=_fake_config()), \
         patch("data_provider.base._trading_minutes_elapsed", return_value=60.0):
        mgr._enrich_intraday_volume_ratio(quote)

    assert quote.volume_ratio is None


def test_enrich_clamps_extreme_values(monkeypatch):
    """异常数据下钳制 ratio 到 [0, 50]。"""
    df = pd.DataFrame({"volume": [1_000] * 5})  # 极小分母
    mgr = _FakeMgr(df=df)
    quote = _make_quote(volume=10_000_000)  # 极大分子

    with patch("src.config.get_config", return_value=_fake_config()), \
         patch("data_provider.base._trading_minutes_elapsed", return_value=60.0):
        mgr._enrich_intraday_volume_ratio(quote)

    assert quote.volume_ratio is not None
    assert 0 <= quote.volume_ratio <= 50
