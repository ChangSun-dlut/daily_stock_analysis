"""Tests for auto-registering minute-level volume alerts after consolidation_breakout screening."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.services.screening_service import _register_consolidation_breakout_minute_alerts


def _make_fake_service() -> MagicMock:
    """Build a fake AlertService whose normalize helper returns a well-formed rule dict."""
    fake = MagicMock()

    def _fake_normalize(payload, *, source="api"):
        return {
            **payload,
            "source": source,
            "severity": "warning",
            "cooldown_policy": None,
            "notification_policy": None,
        }

    fake._normalize_rule_payload.side_effect = _fake_normalize
    fake.repo.create_rule.side_effect = lambda fields: fields
    fake.list_rules.return_value = {"total": 0, "items": []}
    return fake


def test_new_code_is_created_with_normalized_target_and_source():
    fake = _make_fake_service()
    captured = {}

    def _capture(fields):
        captured.update(fields)
        return fields

    fake.repo.create_rule.side_effect = _capture
    with patch("src.services.alert_service.AlertService", return_value=fake):
        stats = _register_consolidation_breakout_minute_alerts(["600519"])

    assert stats["created"] == 1
    assert stats["updated"] == 0
    assert "600519" in captured["target"]  # normalize_stock_code adds exchange suffix
    assert captured["alert_type"] == "volume_spike_rt"
    assert captured["source"] == "consolidation_breakout"
    assert captured["target_scope"] == "single_symbol"
    assert captured["parameters"]["min_ratio"] == 1.2


def test_existing_code_is_updated_not_created():
    fake = _make_fake_service()
    fake.list_rules.return_value = {"total": 1, "items": [{"id": 42}]}
    with patch("src.services.alert_service.AlertService", return_value=fake):
        stats = _register_consolidation_breakout_minute_alerts(["600519"])

    assert stats["updated"] == 1
    assert stats["created"] == 0
    fake.repo.create_rule.assert_not_called()
    fake.update_rule.assert_called_once_with(
        42,
        {
            "enabled": True,
            "parameters": {"window_minutes": 5, "min_ratio": 1.2, "min_slope": 0.0, "min_peak_ratio": 1.3},
            "name": "横盘突破·分钟放量预警 600519",
        },
    )


def test_empty_or_blank_codes_are_skipped():
    fake = _make_fake_service()
    with patch("src.services.alert_service.AlertService", return_value=fake):
        stats = _register_consolidation_breakout_minute_alerts(["", None, "  "])

    assert stats["skipped"] == 3
    assert stats["created"] == 0
    fake.repo.create_rule.assert_not_called()


def test_single_failure_does_not_abort_others():
    fake = _make_fake_service()

    def _boom(fields):
        raise RuntimeError("db down")

    fake.repo.create_rule.side_effect = _boom
    with patch("src.services.alert_service.AlertService", return_value=fake):
        stats = _register_consolidation_breakout_minute_alerts(["600519", "000001"])

    # One raised inside create_rule, the other still attempted; both counted as failed.
    assert stats["failed"] == 2
    assert stats["created"] == 0
