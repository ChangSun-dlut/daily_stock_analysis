# -*- coding: utf-8 -*-
"""回归测试：分钟级实时放量(volume_spike_rt)的盘中短冷却与通知来源标签。

背景：volume_spike_rt 是盘中持续监控的分钟级预警，旧实现复用 24h 默认冷却，
导致盘中触发一次后一整天被压制；且横盘选股自动注册与用户自选股两类通知文案
无法区分。本文件锁定修复后的行为。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.alert_worker import (
    AlertWorker,
    RuntimeAlertRule,
    REALTIME_ALERT_DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_DB_ALERT_COOLDOWN_SECONDS,
)


def _rt_rule(alert_type: str, source: str) -> RuntimeAlertRule:
    return RuntimeAlertRule(
        key=f"{alert_type}:{source}",
        rule=SimpleNamespace(alert_type=alert_type, source=source, description=""),
        source="db",
    )


class RealtimeVolumeSpikeCooldownTestCase(unittest.TestCase):
    def test_realtime_alert_without_policy_uses_short_cooldown(self):
        rule = _rt_rule("volume_spike_rt", "consolidation_breakout")
        self.assertEqual(
            AlertWorker._cooldown_seconds(rule),
            REALTIME_ALERT_DEFAULT_COOLDOWN_SECONDS,
        )

    def test_non_realtime_alert_without_policy_keeps_daily_cooldown(self):
        rule = _rt_rule("ma_price_cross", "user")
        self.assertEqual(
            AlertWorker._cooldown_seconds(rule),
            DEFAULT_DB_ALERT_COOLDOWN_SECONDS,
        )

    def test_explicit_cooldown_policy_is_respected(self):
        rule = _rt_rule("volume_spike_rt", "consolidation_breakout")
        rule.cooldown_policy = {"cooldown_seconds": 600}
        self.assertEqual(AlertWorker._cooldown_seconds(rule), 600)


class RealtimeVolumeSpikeTitleTagTestCase(unittest.TestCase):
    def _capture_alert_text(self, alert_type: str, source: str) -> str:
        notifier = MagicMock()
        worker = AlertWorker(notifier=notifier, service=MagicMock())
        rule = _rt_rule(alert_type, source)
        result = {"reason": "放量上涨预警：600519.SH 量比 1.45x", "direction": "up"}
        with patch.object(AlertWorker, "_alert_historical_win_rate", return_value=None), \
                patch.object(AlertWorker, "_build_analysis_visibility", return_value={}), \
                patch.object(AlertWorker, "_diagnostics_payload", return_value={}):
            worker._send_notification(rule, result)
        return notifier.send_with_results.call_args[0][0]

    def test_consolidation_breakout_tagged(self):
        text = self._capture_alert_text("volume_spike_rt", "consolidation_breakout")
        self.assertIn("横盘突破", text)

    def test_watchlist_tagged(self):
        text = self._capture_alert_text("volume_spike_rt", "user")
        self.assertIn("自选股", text)

    def test_non_realtime_not_tagged(self):
        text = self._capture_alert_text("ma_price_cross", "user")
        self.assertNotIn("横盘突破", text)
        self.assertNotIn("自选股", text)


if __name__ == "__main__":
    unittest.main()
