# -*- coding: utf-8 -*-

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.v1.endpoints import history as history_endpoint


class _FakeHistoryService:
    def __init__(self, result, markdown="# 中钨高新 000657 分析报告"):
        self.result = result
        self.markdown = markdown

    def resolve_and_get_detail(self, record_id):
        return self.result

    def get_markdown_report(self, record_id):
        return self.markdown


def _patch_service(monkeypatch, result, markdown="# 中钨高新 000657 分析报告"):
    service = _FakeHistoryService(result, markdown)
    monkeypatch.setattr(history_endpoint, "HistoryService", lambda _db: service)
    monkeypatch.setattr(
        history_endpoint,
        "get_config",
        lambda: SimpleNamespace(
            markdown_to_image_max_chars=15000,
            md2img_engine="markdown-to-file",
        ),
    )
    return service


def test_history_share_image_returns_png_with_stock_payload(monkeypatch):
    raw_result = {"code": "000657", "name": "中钨高新", "dashboard": {}}
    _patch_service(
        monkeypatch,
        {
            "id": 17,
            "report_type": "detailed",
            "raw_result": raw_result,
            "context_snapshot": {},
        },
    )
    calls = []

    def fake_markdown_to_image(markdown, **kwargs):
        calls.append((markdown, kwargs))
        return b"\x89PNG\r\n\x1a\nposter"

    monkeypatch.setattr(history_endpoint, "markdown_to_image", fake_markdown_to_image)

    response = asyncio.run(history_endpoint.get_history_share_image("17", db_manager=object()))

    assert response.status_code == 200
    assert response.media_type == "image/png"
    assert response.body.startswith(b"\x89PNG")
    assert response.headers["content-disposition"] == 'attachment; filename="dsa-report-17.png"'
    assert calls[0][1]["structured_payload"] is raw_result
    assert calls[0][1]["max_chars"] == 15000


def test_history_share_image_prefers_market_review_payload(monkeypatch):
    market_payload = {"kind": "market_review", "date": "2026-08-01"}
    _patch_service(
        monkeypatch,
        {
            "id": 18,
            "report_type": "market_review",
            "raw_result": {"raw_response": "market report"},
            "context_snapshot": {"market_review_payload": market_payload},
        },
        markdown="# A股市场复盘",
    )
    captured = {}

    def fake_markdown_to_image(markdown, **kwargs):
        captured.update(kwargs)
        return b"png"

    monkeypatch.setattr(history_endpoint, "markdown_to_image", fake_markdown_to_image)

    asyncio.run(history_endpoint.get_history_share_image("18", db_manager=object()))

    assert captured["structured_payload"] is market_payload


def test_history_share_image_html_returns_desktop_poster_with_restrictive_csp(monkeypatch):
    raw_result = {"code": "000657", "name": "中钨高新", "dashboard": {}}
    _patch_service(
        monkeypatch,
        {
            "id": 20,
            "report_type": "detailed",
            "raw_result": raw_result,
            "context_snapshot": {},
        },
    )
    captured = {}

    def fake_build_share_image_html(markdown, **kwargs):
        captured["markdown"] = markdown
        captured.update(kwargs)
        return "<!DOCTYPE html><html><body>poster</body></html>"

    monkeypatch.setattr(
        history_endpoint,
        "build_share_image_html",
        fake_build_share_image_html,
    )

    response = history_endpoint.get_history_share_image_html("20", db_manager=object())

    assert response.status_code == 200
    assert response.media_type == "text/html"
    assert b"poster" in response.body
    assert captured["structured_payload"] is raw_result
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
    )


def test_history_share_image_html_rejects_reports_over_configured_limit(monkeypatch):
    _patch_service(
        monkeypatch,
        {
            "id": 21,
            "report_type": "detailed",
            "raw_result": {"code": "000657"},
            "context_snapshot": {},
        },
        markdown="x" * 15001,
    )

    with pytest.raises(HTTPException) as exc_info:
        history_endpoint.get_history_share_image_html("21", db_manager=object())

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail["error"] == "share_image_too_large"


def test_history_share_image_reports_renderer_unavailable(monkeypatch):
    _patch_service(
        monkeypatch,
        {
            "id": 19,
            "report_type": "detailed",
            "raw_result": {"code": "000657"},
            "context_snapshot": {},
        },
    )
    monkeypatch.setattr(history_endpoint, "markdown_to_image", lambda *_args, **_kwargs: None)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(history_endpoint.get_history_share_image("19", db_manager=object()))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"] == "share_image_unavailable"


def test_history_share_image_returns_not_found(monkeypatch):
    _patch_service(monkeypatch, None)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(history_endpoint.get_history_share_image("missing", db_manager=object()))

    assert exc_info.value.status_code == 404


# --- 批量分享图推送到微信（OpenClaw） ---

class _FakePoster:
    code = "000657"
    name = "中钨高新"


class _FakeSender:
    def __init__(self, config=None):
        self.config = config
        self.sent = None

    def _send_openclaw_wechat_image(self, image_bytes):
        self.sent = image_bytes
        return True


def _patch_push_service(monkeypatch, sender_cls=_FakeSender):
    monkeypatch.setattr(
        history_endpoint,
        "_batch_poster_from_history",
        lambda *args, **kwargs: _FakePoster(),
    )
    monkeypatch.setattr(
        history_endpoint,
        "build_batch_share_image_html",
        lambda *args, **kwargs: "<html></html>",
    )
    monkeypatch.setattr(history_endpoint, "html_to_image", lambda *args, **kwargs: b"\x89PNG fake")
    monkeypatch.setattr(history_endpoint, "HistoryService", lambda _db: object())
    monkeypatch.setattr(
        history_endpoint,
        "get_config",
        lambda: SimpleNamespace(md2img_engine="markdown-to-file"),
    )
    import src.notification_sender.openclaw_wechat_sender as ocw_mod

    monkeypatch.setattr(ocw_mod, "OpenclawWechatSender", sender_cls)


def test_batch_share_image_push_succeeds(monkeypatch):
    sent = {}

    class _Sender(_FakeSender):
        def _send_openclaw_wechat_image(self, image_bytes):
            sent["bytes"] = image_bytes
            return True

    _patch_push_service(monkeypatch, _Sender)
    req = SimpleNamespace(record_ids=[1, 2], cards_per_row=3, channel="openclaw_wechat")

    result = asyncio.run(history_endpoint.post_batch_share_image_push(req, db_manager=object()))

    assert result.success is True
    assert result.pushed is True
    assert "OpenClaw" in result.message
    assert sent["bytes"] == b"\x89PNG fake"


def test_batch_share_image_push_requires_two_records(monkeypatch):
    _patch_push_service(monkeypatch)
    req = SimpleNamespace(record_ids=[1], cards_per_row=3, channel="openclaw_wechat")

    result = asyncio.run(history_endpoint.post_batch_share_image_push(req, db_manager=object()))

    assert result.success is False
    assert result.pushed is False
    assert "2" in result.message


def test_batch_share_image_push_rejects_unsupported_channel(monkeypatch):
    _patch_push_service(monkeypatch)
    req = SimpleNamespace(record_ids=[1, 2], cards_per_row=3, channel="telegram")

    result = asyncio.run(history_endpoint.post_batch_share_image_push(req, db_manager=object()))

    assert result.success is False
    assert result.pushed is False
    assert "openclaw_wechat" in result.message


def test_batch_share_image_push_fails_when_sender_returns_false(monkeypatch):
    class _Sender(_FakeSender):
        def _send_openclaw_wechat_image(self, image_bytes):
            return False

    _patch_push_service(monkeypatch, _Sender)
    req = SimpleNamespace(record_ids=[1, 2], cards_per_row=3, channel="openclaw_wechat")

    result = asyncio.run(history_endpoint.post_batch_share_image_push(req, db_manager=object()))

    assert result.success is False
    assert result.pushed is False
    assert "OpenClaw" in result.message


def test_batch_share_image_push_fails_when_render_fails(monkeypatch):
    _patch_push_service(monkeypatch)
    monkeypatch.setattr(history_endpoint, "html_to_image", lambda *args, **kwargs: None)
    req = SimpleNamespace(record_ids=[1, 2], cards_per_row=3, channel="openclaw_wechat")

    result = asyncio.run(history_endpoint.post_batch_share_image_push(req, db_manager=object()))

    assert result.success is False
    assert result.pushed is False


# --- 单条分享图推送到微信（OpenClaw） ---

def _patch_single_push_service(monkeypatch, sender_cls=_FakeSender, image_bytes=b"\x89PNG single"):
    monkeypatch.setattr(
        history_endpoint,
        "get_config",
        lambda: SimpleNamespace(
            markdown_to_image_max_chars=15000,
            md2img_engine="markdown-to-file",
        ),
    )
    monkeypatch.setattr(
        history_endpoint,
        "_history_share_image_input",
        lambda _record_id, _db: ({"code": "000657"}, "# 报告"),
    )
    monkeypatch.setattr(history_endpoint, "markdown_to_image", lambda *args, **kwargs: image_bytes)
    import src.notification_sender.openclaw_wechat_sender as ocw_mod

    monkeypatch.setattr(ocw_mod, "OpenclawWechatSender", sender_cls)


def test_history_single_share_image_push_succeeds(monkeypatch):
    sent = {}

    class _Sender(_FakeSender):
        def _send_openclaw_wechat_image(self, image_bytes):
            sent["bytes"] = image_bytes
            return True

    _patch_single_push_service(monkeypatch, _Sender)

    result = asyncio.run(history_endpoint.push_history_share_image("17", channel="openclaw_wechat", db_manager=object()))

    assert result.success is True
    assert result.pushed is True
    assert "OpenClaw" in result.message
    assert sent["bytes"] == b"\x89PNG single"


def test_history_single_share_image_push_rejects_unsupported_channel(monkeypatch):
    _patch_single_push_service(monkeypatch)

    result = asyncio.run(history_endpoint.push_history_share_image("17", channel="telegram", db_manager=object()))

    assert result.success is False
    assert result.pushed is False
    assert "openclaw_wechat" in result.message


def test_history_single_share_image_push_fails_when_sender_returns_false(monkeypatch):
    class _Sender(_FakeSender):
        def _send_openclaw_wechat_image(self, image_bytes):
            return False

    _patch_single_push_service(monkeypatch, _Sender)

    result = asyncio.run(history_endpoint.push_history_share_image("17", channel="openclaw_wechat", db_manager=object()))

    assert result.success is False
    assert result.pushed is False
    assert "OpenClaw" in result.message


def test_history_single_share_image_push_fails_when_render_fails(monkeypatch):
    _patch_single_push_service(monkeypatch, image_bytes=None)

    result = asyncio.run(history_endpoint.push_history_share_image("17", channel="openclaw_wechat", db_manager=object()))

    assert result.success is False
    assert result.pushed is False
    assert "markdown-to-file" in result.message
