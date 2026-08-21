"""OpenClaw 微信发送器单元测试（subprocess / socket 已 mock）。"""
from unittest import mock

import pytest

from src.notification_sender.openclaw_wechat_sender import OpenclawWechatSender


def _make_sender(monkeypatch, run_returncode=0, run_stdout=None, run_stderr=""):
    cfg = mock.Mock()
    cfg.openclaw_wechat_account = "f4add82e8cf4-im-bot"
    cfg.openclaw_wechat_target = "o9cq803K8OvJ6mTb0qsWMDB9dtfM@im.wechat"
    cfg.openclaw_cli_bin = ""
    cfg.openclaw_gateway_port = 18789
    svc = OpenclawWechatSender(cfg)

    def fake_run(cmd, **kwargs):
        class P:
            returncode = run_returncode
            stdout = run_stdout if run_stdout is not None else '{"deliveryStatus":"sent"}'
            stderr = run_stderr

        return P()

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )
    # 默认假设 gateway 在线，避免测试触发真实 TCP 探测/重启。
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: True)
    return svc


def _unconfigured_sender():
    cfg = mock.Mock()
    cfg.openclaw_wechat_account = ""
    cfg.openclaw_wechat_target = ""
    cfg.openclaw_cli_bin = ""
    cfg.openclaw_gateway_port = 18789
    return OpenclawWechatSender(cfg)


def test_send_text_success(monkeypatch):
    svc = _make_sender(monkeypatch)
    assert svc.send_to_openclaw_wechat("你好微信") is True


def test_send_text_success_nested_payload(monkeypatch):
    svc = _make_sender(
        monkeypatch,
        run_stdout='{"payload":{"deliveryStatus":"sent"}}',
    )
    assert svc.send_to_openclaw_wechat("你好微信") is True


def test_send_text_failure_returncode(monkeypatch):
    svc = _make_sender(monkeypatch, run_returncode=1, run_stderr="boom")
    assert svc.send_to_openclaw_wechat("你好微信") is False


def test_send_text_unconfigured():
    svc = _unconfigured_sender()
    assert svc.send_to_openclaw_wechat("hi") is False


def test_send_image_uses_media_flag(monkeypatch):
    captured = {}
    svc = _make_sender(monkeypatch)
    real_run = svc._run_cli

    def spy(extra_args, timeout_seconds):
        captured["args"] = list(extra_args)
        return real_run(extra_args, timeout_seconds)

    monkeypatch.setattr(svc, "_run_cli", spy)
    png = b"\x89PNG\r\n\x1a\n fake"
    assert svc._send_openclaw_wechat_image(png) is True
    assert "--media" in captured["args"]


def test_send_image_disabled():
    svc = _unconfigured_sender()
    assert svc._send_openclaw_wechat_image(b"\x89PNG") is False


# ----------------------------------------------------------------------
# gateway 重启兜底
# ----------------------------------------------------------------------
def _raw_sender():
    cfg = mock.Mock()
    cfg.openclaw_wechat_account = "f4add82e8cf4-im-bot"
    cfg.openclaw_wechat_target = "o9cq803K8OvJ6mTb0qsWMDB9dtfM@im.wechat"
    cfg.openclaw_cli_bin = ""
    cfg.openclaw_gateway_port = 18789
    return OpenclawWechatSender(cfg)


def test_gateway_reachable_true(monkeypatch):
    svc = _raw_sender()

    class Cm:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.socket.create_connection",
        lambda *a, **k: Cm(),
    )
    assert svc._gateway_reachable() is True


def test_gateway_reachable_false(monkeypatch):
    svc = _raw_sender()
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.socket.create_connection",
        mock.Mock(side_effect=OSError("down")),
    )
    assert svc._gateway_reachable() is False


def test_restart_gateway_runs_run_force_first(monkeypatch):
    svc = _make_sender(monkeypatch)
    service_calls = []
    popen = mock.Mock()

    def fake_run(cmd, **kwargs):
        service_calls.append(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.Popen", popen
    )
    assert svc._restart_gateway() is True
    # 优先后台拉起 run --force
    assert popen.called
    args = popen.call_args[0][0]
    assert "run" in args and "--force" in args and "--bind" in args
    # 未走到 restart/start 服务路径
    assert service_calls == []


def test_restart_gateway_falls_back_to_service(monkeypatch):
    svc = _make_sender(monkeypatch)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    popen = mock.Mock(side_effect=OSError("cannot spawn"))
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.Popen", popen
    )
    assert svc._restart_gateway() is True
    # run --force 启动失败，回退到 restart/start 服务路径（restart 成功即返回）
    assert any(c[-1] == "restart" for c in calls)


def test_restart_gateway_start_after_restart_fails(monkeypatch):
    svc = _make_sender(monkeypatch)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # restart 失败，start 成功
        return mock.Mock(returncode=0 if cmd[-1] == "start" else 1, stdout="", stderr="")

    popen = mock.Mock(side_effect=OSError("cannot spawn"))
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.Popen", popen
    )
    assert svc._restart_gateway() is True
    assert any(c[-1] == "restart" for c in calls)
    assert any(c[-1] == "start" for c in calls)


def test_restart_gateway_all_fail(monkeypatch):
    svc = _make_sender(monkeypatch)

    def fake_run(cmd, **kwargs):
        return mock.Mock(returncode=1, stdout="", stderr="fail")

    popen = mock.Mock(side_effect=OSError("cannot spawn"))
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.Popen", popen
    )
    assert svc._restart_gateway() is False


def test_send_retries_after_gateway_restart(monkeypatch):
    svc = _make_sender(monkeypatch)
    state = {"reachable": 0}

    def fake_reachable():
        state["reachable"] += 1
        # 首次探测为离线；重启后轮询变为在线。
        return state["reachable"] > 1

    monkeypatch.setattr(svc, "_gateway_reachable", fake_reachable)
    restarted = {"n": 0}

    def fake_restart():
        restarted["n"] += 1
        return True

    monkeypatch.setattr(svc, "_restart_gateway", fake_restart)
    assert svc.send_to_openclaw_wechat("hi") is True
    assert restarted["n"] == 1


def test_send_false_when_gateway_cannot_restart(monkeypatch):
    svc = _make_sender(monkeypatch)
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: False)
    monkeypatch.setattr(svc, "_restart_gateway", lambda: False)
    assert svc.send_to_openclaw_wechat("hi") is False


def test_send_image_false_when_gateway_cannot_restart(monkeypatch):
    svc = _make_sender(monkeypatch)
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: False)
    monkeypatch.setattr(svc, "_restart_gateway", lambda: False)
    assert svc._send_openclaw_wechat_image(b"\x89PNG") is False
