# -*- coding: utf-8 -*-
"""Unit tests for PytdxFetcher realtime contract (no network)."""

import unittest
from contextlib import contextmanager
from unittest.mock import patch

import pandas as pd

from data_provider.pytdx_fetcher import PytdxFetcher
from data_provider.realtime_types import RealtimeSource


class _FakeApi:
    def get_security_quotes(self, _codes):
        return [{
            "name": "贵州茅台",
            "price": 1302.8,
            "open": 1304.0,
            "high": 1310.0,
            "low": 1300.0,
            "last_close": 1304.0,
            "vol": 21730,
            "amount": 28200000.0,
            "datetime": "2026-08-26 15:00",
        }]

    def get_security_bars(self, **_kw):
        return [
            {"datetime": "2026-08-26 14:58", "open": 1303.5, "close": 1303.5,
             "high": 1303.5, "low": 1303.5, "vol": 100, "amount": 130355.0},
            {"datetime": "2026-08-26 15:00", "open": 1302.8, "close": 1302.8,
             "high": 1302.8, "low": 1302.8, "vol": 26000, "amount": 33872800.0},
        ]

    def to_df(self, raw):
        return pd.DataFrame(raw)


@contextmanager
def _fake_session(self):
    yield _FakeApi()


class TestPytdxFetcherRealtime(unittest.TestCase):
    def setUp(self):
        self.fetcher = PytdxFetcher.__new__(PytdxFetcher)
        self.fetcher.logger = __import__("logging").getLogger("test")
        self.fetcher.hosts = [("127.0.0.1", 7709)]

    @patch.object(PytdxFetcher, "_pytdx_session", _fake_session)
    def test_get_realtime_quote_returns_unified(self):
        from data_provider.realtime_types import UnifiedRealtimeQuote
        q = self.fetcher.get_realtime_quote("600519")
        self.assertIsInstance(q, UnifiedRealtimeQuote)
        self.assertEqual(q.source, RealtimeSource.PYTDX)
        self.assertEqual(q.code, "600519")
        self.assertEqual(q.price, 1302.8)
        self.assertEqual(q.pre_close, 1304.0)
        self.assertEqual(q.volume, 21730)

    @patch.object(PytdxFetcher, "_pytdx_session", _fake_session)
    def test_get_intraday_bars_normalizes_columns(self):
        df = self.fetcher.get_intraday_bars("600519", count=5)
        self.assertIsInstance(df, pd.DataFrame)
        self.assertIn("volume", df.columns)
        self.assertIn("time", df.columns)
        self.assertEqual(len(df), 2)

    def test_rejects_bse_code(self):
        with self.assertRaises(Exception):
            self.fetcher.get_realtime_quote("831370")


if __name__ == "__main__":
    unittest.main()
