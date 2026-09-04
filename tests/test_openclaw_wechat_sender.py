"""OpenClaw 微信发送器单元测试（subprocess / socket 已 mock）。"""
import json
import os
import time
from unittest import mock

import pytest

from src.notification_sender.openclaw_wechat_sender import OpenclawWechatSender


def _reset_shared_cooldowns() -> None:
    """清空 sender 的**类级**冷却状态，保证用例之间互相隔离。

    这些时间戳自 2026-08-31 起提升为类属性（生产上发送链路/巡检每次都新建
    sender 实例，用实例变量会让冷却永远归零、gateway 被反复重启）。代价是状态
    会跨用例残留，测试必须在每个用例开始时清零。
    """
    OpenclawWechatSender._last_restart_attempt = 0.0
    OpenclawWechatSender._last_context_clear_ts = 0.0
    OpenclawWechatSender._last_context_failure_ts = 0.0
    OpenclawWechatSender._last_crash_loop_restart_ts = 0.0


def _make_sender(monkeypatch, run_returncode=0, run_stdout=None, run_stderr=""):
    _reset_shared_cooldowns()
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
    # 失败后会自动修复并重试；mock 掉修复动作避免真实拉起 gateway。
    monkeypatch.setattr(svc, "_repair_service", lambda *, force_clear_token=False, restart_gateway=True: None)
    assert svc.send_to_openclaw_wechat("你好微信") is False


def test_send_text_self_heals_after_repair(monkeypatch):
    svc = _make_sender(monkeypatch)
    attempts = {"n": 0}
    real_run = svc._run_cli

    def flaky(extra_args, timeout_seconds=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return False  # 首次失败（如 iLink bot 离线）
        return real_run(extra_args, timeout_seconds)  # 修复后成功

    monkeypatch.setattr(svc, "_run_cli", flaky)
    monkeypatch.setattr(svc, "_repair_service", lambda *, force_clear_token=False, restart_gateway=True: None)
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: True)
    assert svc.send_to_openclaw_wechat("hi") is True
    assert attempts["n"] == 2


def test_send_text_no_repair_when_auto_repair_false(monkeypatch):
    svc = _make_sender(monkeypatch)
    calls = {"run": 0}

    def noop_run(extra_args, timeout_seconds=None):
        calls["run"] += 1
        return False

    monkeypatch.setattr(svc, "_run_cli", noop_run)
    assert svc.send_to_openclaw_wechat("hi", auto_repair=False) is False
    assert calls["run"] == 1


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


def _fake_run_unmanaged(calls, *, restart_rc=0, start_rc=0):
    """构造 subprocess.run 桩：launchctl 一律返回「未托管」，走非 launchd 路径。"""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd and cmd[0] == "launchctl":
            # 无 launchd 服务（非托管场景）
            return mock.Mock(returncode=1, stdout="", stderr="")
        if cmd and cmd[-1] == "restart":
            return mock.Mock(returncode=restart_rc, stdout="", stderr="")
        if cmd and cmd[-1] == "start":
            return mock.Mock(returncode=start_rc, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")

    return fake_run


def test_restart_gateway_prefers_launchctl_when_managed(monkeypatch):
    """launchd 托管时必须走 kickstart，禁止 run --force。

    2026-08-31 事故回归测试：gateway 由 LaunchAgent（KeepAlive）托管时再用
    `gateway run --force` 会与已运行实例抢 18789 端口 → EADDRINUSE → unclean boot
    → 3 次/5 分钟打满 crash-loop breaker → 通道被禁自启，自愈反而把通道搞挂。
    """
    svc = _make_sender(monkeypatch)
    calls = []
    popen = mock.Mock()

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.Popen", popen
    )
    assert svc._restart_gateway() is True
    # 先探测 launchctl list，再用 kickstart -k 重启
    assert any(c[:2] == ["launchctl", "list"] for c in calls)
    assert any(c[:3] == ["launchctl", "kickstart", "-k"] for c in calls)
    # 关键：绝不能再拉起 run --force
    assert not popen.called


def test_restart_gateway_runs_run_force_first(monkeypatch):
    """未托管场景：回退到后台拉起 run --force。"""
    svc = _make_sender(monkeypatch)
    service_calls = []
    popen = mock.Mock()

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run",
        _fake_run_unmanaged(service_calls),
    )
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.Popen", popen
    )
    assert svc._restart_gateway() is True
    # 后台拉起 run --force
    assert popen.called
    args = popen.call_args[0][0]
    assert "run" in args and "--force" in args and "--bind" in args
    # 未走到 openclaw gateway restart/start 服务路径
    assert not any(
        c[0] == "openclaw" and c[-1] in ("restart", "start") for c in service_calls
    )


def test_restart_gateway_falls_back_to_service(monkeypatch):
    svc = _make_sender(monkeypatch)
    calls = []

    popen = mock.Mock(side_effect=OSError("cannot spawn"))
    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run",
        _fake_run_unmanaged(calls),
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


def test_send_image_self_heals_after_repair(monkeypatch):
    svc = _make_sender(monkeypatch)
    attempts = {"n": 0}
    real_run = svc._run_cli

    def flaky(extra_args, timeout_seconds=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return False  # 首次失败（如 iLink bot 未在线）
        return real_run(extra_args, timeout_seconds)  # 自动修复后成功

    monkeypatch.setattr(svc, "_run_cli", flaky)
    monkeypatch.setattr(svc, "_restart_gateway", lambda: True)
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: True)
    assert svc._send_openclaw_wechat_image(b"\x89PNG") is True
    assert attempts["n"] == 2
    assert svc.last_repair_info is not None
    assert svc.last_repair_info["gateway_restarted"] is True


def test_send_image_reports_bot_login_required(monkeypatch):
    svc = _make_sender(monkeypatch)
    monkeypatch.setattr(svc, "_run_cli", lambda extra_args, timeout_seconds=None: False)
    monkeypatch.setattr(svc, "_restart_gateway", lambda: True)
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: True)
    monkeypatch.setattr(svc, "_bot_healthy", lambda timeout_seconds=10: False)
    assert svc._send_openclaw_wechat_image(b"\x89PNG") is False
    assert svc.last_repair_info is not None
    assert svc.last_repair_info["bot_healthy"] is False
    assert svc.last_repair_info["gateway_restarted"] is True


def test_send_image_no_repair_when_auto_repair_false(monkeypatch):
    svc = _make_sender(monkeypatch)
    calls = {"run": 0}

    def noop_run(extra_args, timeout_seconds=None):
        calls["run"] += 1
        return False

    monkeypatch.setattr(svc, "_run_cli", noop_run)
    assert svc._send_openclaw_wechat_image(b"\x89PNG", auto_repair=False) is False
    assert calls["run"] == 1
    assert svc.last_repair_info is None


# ----------------------------------------------------------------------
# 错误分类 + 主动探活
# ----------------------------------------------------------------------
def test_classify_auth_needed(monkeypatch):
    from src.notification_sender.openclaw_wechat_sender import (
        classify_openclaw_send_error,
        OPENCLAW_ERR_AUTH_NEEDED,
    )

    assert (
        classify_openclaw_send_error(
            returncode=1, stderr="sendMessage ret=-2 errmsg=prepare failed"
        )
        == OPENCLAW_ERR_AUTH_NEEDED
    )
    assert (
        classify_openclaw_send_error(returncode=0, stderr="需要重新登录")
        == OPENCLAW_ERR_AUTH_NEEDED
    )


def test_classify_cdn_error(monkeypatch):
    from src.notification_sender.openclaw_wechat_sender import (
        classify_openclaw_send_error,
        OPENCLAW_ERR_CDN,
    )

    assert (
        classify_openclaw_send_error(
            returncode=0,
            stderr="OutboundDeliveryError: CDN upload server error: status 500",
        )
        == OPENCLAW_ERR_CDN
    )


def test_classify_gateway_down(monkeypatch):
    from src.notification_sender.openclaw_wechat_sender import (
        classify_openclaw_send_error,
        OPENCLAW_ERR_GATEWAY,
    )

    assert (
        classify_openclaw_send_error(returncode=1, stderr="WebSocket closed before message sent")
        == OPENCLAW_ERR_GATEWAY
    )
    assert (
        classify_openclaw_send_error(returncode=1, stderr="connect ECONNREFUSED 127.0.0.1:18789")
        == OPENCLAW_ERR_GATEWAY
    )


def test_classify_timeout(monkeypatch):
    from src.notification_sender.openclaw_wechat_sender import (
        classify_openclaw_send_error,
        OPENCLAW_ERR_TIMEOUT,
    )

    assert (
        classify_openclaw_send_error(returncode=1, stderr="anything", timed_out=True)
        == OPENCLAW_ERR_TIMEOUT
    )


def test_classify_other_and_ok(monkeypatch):
    from src.notification_sender.openclaw_wechat_sender import (
        classify_openclaw_send_error,
        OPENCLAW_ERR_OTHER,
        OPENCLAW_ERR_OK,
    )

    assert (
        classify_openclaw_send_error(returncode=1, stderr="unknown failure")
        == OPENCLAW_ERR_OTHER
    )
    assert classify_openclaw_send_error(returncode=0, stderr="") == OPENCLAW_ERR_OK


def test_run_cli_records_auth_needed_error(monkeypatch):
    svc = _make_sender(
        monkeypatch,
        run_returncode=1,
        run_stderr="sendMessage ret=-2 errmsg=prepare failed",
    )
    monkeypatch.setattr(svc, "_repair_service", lambda *, force_clear_token=False, restart_gateway=True: None)
    assert svc.send_to_openclaw_wechat("hi") is False
    from src.notification_sender.openclaw_wechat_sender import OPENCLAW_ERR_AUTH_NEEDED
    assert svc.last_error_type == OPENCLAW_ERR_AUTH_NEEDED
    assert "prepare failed" in svc.last_error_detail


def test_run_cli_records_cdn_error(monkeypatch):
    svc = _make_sender(
        monkeypatch,
        run_returncode=1,
        run_stderr="OutboundDeliveryError: CDN upload server error: status 500",
    )
    monkeypatch.setattr(svc, "_repair_service", lambda *, force_clear_token=False, restart_gateway=True: None)
    assert svc._send_openclaw_wechat_image(b"\x89PNG") is False
    from src.notification_sender.openclaw_wechat_sender import OPENCLAW_ERR_CDN
    assert svc.last_error_type == OPENCLAW_ERR_CDN


def test_check_bot_health_unconfigured():
    svc = _unconfigured_sender()
    health = svc.check_bot_health()
    assert health["enabled"] is False
    assert health["needs_relogin"] is False


def test_check_bot_health_healthy(monkeypatch):
    svc = _make_sender(monkeypatch)
    # 首次调用 channels status（在 _bot_healthy 里），再次调用在 detail 抓 lastError
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: True)
    monkeypatch.setattr(svc, "_bot_healthy", lambda timeout_seconds=10: True)

    healthy_payload = json.dumps({
        "channelAccounts": {
            "openclaw-weixin": [
                {
                    "id": svc._account,
                    "running": True,
                    "lastError": None,
                }
            ]
        }
    })

    def fake_run(cmd, **kwargs):
        return mock.Mock(returncode=0, stdout=healthy_payload, stderr="")

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )

    health = svc.check_bot_health()
    assert health["enabled"] is True
    assert health["gateway_reachable"] is True
    assert health["bot_healthy"] is True
    assert health["needs_relogin"] is False
    assert health["last_error"] is None


def test_check_bot_health_needs_relogin(monkeypatch):
    svc = _make_sender(monkeypatch)
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: True)
    monkeypatch.setattr(svc, "_bot_healthy", lambda timeout_seconds=10: False)

    relogin_payload = json.dumps({
        "channelAccounts": {
            "openclaw-weixin": [
                {
                    "id": svc._account,
                    "running": False,
                    "lastError": "iLink bot needs relogin: token expired",
                }
            ]
        }
    })

    def fake_run(cmd, **kwargs):
        return mock.Mock(returncode=0, stdout=relogin_payload, stderr="")

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )

    health = svc.check_bot_health()
    assert health["needs_relogin"] is True
    assert health["bot_healthy"] is False
    assert "relogin" in (health["last_error"] or "")
    assert "relogin" in health["detail"]


# ----------------------------------------------------------------------
# 旁路巡检自动修复：gateway 拉起 + iLink contextToken 失效自愈
# ----------------------------------------------------------------------
def test_clear_stale_context_tokens_deletes_when_force(monkeypatch, tmp_path):
    svc = _make_sender(monkeypatch)
    token = tmp_path / "tok.context-tokens.json"
    token.write_text("{}")
    monkeypatch.setattr(svc, "_context_tokens_path", lambda: str(token))
    assert svc._clear_stale_context_tokens(force=True) is True
    assert not token.exists()


def test_clear_stale_context_tokens_skips_fresh_token(monkeypatch, tmp_path):
    svc = _make_sender(monkeypatch)
    token = tmp_path / "tok.context-tokens.json"
    token.write_text("{}")
    # 记录一次“已知失败时刻”，并使 token 文件 mtime 晚于该时刻（用户刚重建）。
    svc._last_context_failure_ts = time.time()
    future = time.time() + 10
    os.utime(str(token), (future, future))
    monkeypatch.setattr(svc, "_context_tokens_path", lambda: str(token))
    # force=False 时应保护刚重建的 token，不删除。
    assert svc._clear_stale_context_tokens(force=False) is False
    assert token.exists()


def test_clear_stale_context_tokens_no_file_is_noop(monkeypatch, tmp_path):
    svc = _make_sender(monkeypatch)
    monkeypatch.setattr(svc, "_context_tokens_path", lambda: str(tmp_path / "missing.json"))
    assert svc._clear_stale_context_tokens(force=True) is False


def test_repair_service_clears_context_token_on_prepare_failed(monkeypatch, tmp_path):
    svc = _make_sender(monkeypatch)
    from src.notification_sender.openclaw_wechat_sender import OPENCLAW_ERR_AUTH_NEEDED

    token = tmp_path / "tok.context-tokens.json"
    token.write_text("{}")
    monkeypatch.setattr(svc, "_context_tokens_path", lambda: str(token))
    restart_calls = []
    monkeypatch.setattr(
        svc, "_restart_gateway", lambda: restart_calls.append(True) or True
    )
    monkeypatch.setattr(svc, "_bot_healthy", lambda timeout_seconds=10: True)
    svc.last_error_type = OPENCLAW_ERR_AUTH_NEEDED
    svc.last_error_detail = "sendMessage ret=-2 errmsg=prepare failed"

    info = svc._repair_service(force_clear_token=True)
    assert info["context_token_cleared"] is True
    # channels.start 成功（(a) 临时掉线）时 _repair_service 不在此标记 needs_user_message，
    # 交由发送方重试判断；仅当 channels.start 失败（(b) contextToken 彻底 missing）才标记。
    assert info["needs_user_message"] is False
    # 2026-08-31 契约变更：prepare failed 时**绝不重启** gateway。
    # token 只能由接收人发消息重建，重启治不了，反而冲断 iLink 会话、累积
    # unclean boot（实测 1 小时 47 次重启 → gateway 被推进 crash-loop breaker）。
    assert restart_calls == []
    assert info["gateway_restarted"] is False
    assert not token.exists()


def test_clear_stale_context_tokens_skips_fresh_token(monkeypatch, tmp_path):
    """force_clear_token=False 时，mtime 不早于最近失败时刻的 token 不被误删。

    2026-09-04 加固点：发送链路失败重试改为默认 force=False 走 mtime 护栏，
    避免把临时 (a) iLink 掉线误判成 (b) contextToken 失效并主动制造 token 失效。
    """
    svc = _make_sender(monkeypatch)
    token = tmp_path / "tok.context-tokens.json"
    token.write_text("{}")
    monkeypatch.setattr(svc, "_context_tokens_path", lambda: str(token))
    # 模拟“用户刚发消息重建了 token，随后才检测到 prepare failed”：失败时刻记录在前，
    # 之后重建的 token mtime 晚于失败时刻 → 护栏跳过，保留用户刚重建的 token。
    fail_ts = time.time() - 5.0
    type(svc)._last_context_failure_ts = fail_ts
    time.sleep(0.05)
    token.write_text("{}")  # 重建 token，mtime 晚于失败时刻
    # force=False 走护栏：token mtime >= 失败时刻 → 跳过，保留用户刚重建的 token。
    assert svc._clear_stale_context_tokens(force=False) is False
    assert token.exists()


def test_send_text_prepare_failed_auto_clears_token_and_stops(monkeypatch, tmp_path):
    svc = _make_sender(
        monkeypatch,
        run_returncode=1,
        run_stderr="sendMessage ret=-2 errmsg=prepare failed",
    )
    from src.notification_sender.openclaw_wechat_sender import OPENCLAW_ERR_AUTH_NEEDED

    token = tmp_path / "tok.context-tokens.json"
    token.write_text("{}")
    monkeypatch.setattr(svc, "_context_tokens_path", lambda: str(token))
    monkeypatch.setattr(svc, "_restart_gateway", lambda: True)
    monkeypatch.setattr(svc, "_bot_healthy", lambda timeout_seconds=10: True)

    # 统计“message send”调用次数，并让 channels status 返回健康，避免真实网络。
    send_calls = {"n": 0}
    healthy_payload = json.dumps({
        "channelAccounts": {"openclaw-weixin": [{"id": svc._account, "running": True, "lastError": None}]}
    })

    def spy_run(cmd, **kwargs):
        if "channels" in cmd:
            return mock.Mock(returncode=0, stdout=healthy_payload, stderr="")
        send_calls["n"] += 1
        return mock.Mock(returncode=1, stdout="", stderr="sendMessage ret=-2 errmsg=prepare failed")

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", spy_run
    )

    assert svc.send_to_openclaw_wechat("hi") is False
    # auto_repair 会清 token 后由发送方重试一次（首次失败 + 修复后重试），故发起两次 message send。
    assert send_calls["n"] == 2
    assert svc.last_needs_user_message is True
    assert svc.last_error_type == OPENCLAW_ERR_AUTH_NEEDED
    assert not token.exists()


def test_check_bot_health_auto_heal_context_issue(monkeypatch, tmp_path):
    svc = _make_sender(monkeypatch)
    from src.notification_sender.openclaw_wechat_sender import OPENCLAW_ERR_AUTH_NEEDED

    token = tmp_path / "tok.context-tokens.json"
    token.write_text("{}")
    monkeypatch.setattr(svc, "_context_tokens_path", lambda: str(token))
    monkeypatch.setattr(svc, "_restart_gateway", lambda: True)
    monkeypatch.setattr(svc, "_bot_healthy", lambda timeout_seconds=10: True)
    svc.last_error_type = OPENCLAW_ERR_AUTH_NEEDED
    svc.last_error_detail = "sendMessage ret=-2 errmsg=prepare failed"

    health = svc.check_bot_health(auto_heal=True)
    # contextToken 失效已清 token（context_token_cleared）；channels.start 成功时不在此标记
    # needs_user_message，交由发送方在重试失败时设置（新 auto_repair 语义）。
    assert health["needs_user_message"] is False
    assert health["context_token_cleared"] is True
    assert health["repaired"] is True
    # contextToken 失效不是授权过期，不应误报需重扫。
    assert health["needs_relogin"] is False
    assert not token.exists()


def test_check_bot_health_auto_heal_healthy_no_restart(monkeypatch):
    svc = _make_sender(monkeypatch)
    monkeypatch.setattr(svc, "_bot_healthy", lambda timeout_seconds=10: True)
    restarted = {"n": 0}

    def fake_restart():
        restarted["n"] += 1
        return True

    monkeypatch.setattr(svc, "_restart_gateway", fake_restart)

    health = svc.check_bot_health(auto_heal=True)
    # 健康且无 context 故障时，不应反复重启一个在线 gateway。
    assert restarted["n"] == 0
    assert health["repaired"] is False
    assert health["needs_user_message"] is False


def test_crash_loop_detection(monkeypatch):
    svc = _make_sender(monkeypatch)
    # channels status 返回 crash-loop breaker 抑制文案（端口仍可达）。
    crash_payload = json.dumps({
        "channelAccounts": {
            "openclaw-weixin": [
                {
                    "accountId": "f4add82e8cf4-im-bot",
                    "lastError": "gateway restart-loop breaker tripped: 3 unclean boot(s)",
                    "running": False,
                    "configured": True,
                    "enabled": True,
                }
            ]
        }
    })

    def fake_run(cmd, **kwargs):
        class P:
            returncode = 0
            stdout = crash_payload
            stderr = ""

        return P()

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )
    # 端口仍可达（gateway 进程在跑），但通道被 breaker 抑制。
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: True)
    assert svc._crash_loop_suppressed() is True


def test_check_bot_health_auto_heal_crash_loop_restarts(monkeypatch):
    svc = _make_sender(monkeypatch)
    crash_payload = json.dumps({
        "channelAccounts": {
            "openclaw-weixin": [
                {
                    "accountId": "f4add82e8cf4-im-bot",
                    "id": "f4add82e8cf4-im-bot",
                    "lastError": "gateway restart-loop breaker tripped: 3 unclean boot(s)",
                    "running": False,
                    "configured": True,
                    "enabled": True,
                }
            ]
        }
    })

    def fake_run(cmd, **kwargs):
        class P:
            returncode = 0
            stdout = crash_payload
            stderr = ""

        return P()

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )
    monkeypatch.setattr(svc, "_gateway_reachable", lambda: True)
    restarted = {"n": 0}

    def fake_restart():
        restarted["n"] += 1
        return True

    monkeypatch.setattr(svc, "_restart_gateway", fake_restart)
    # _wait_for_gateway_ready 会轮询，mock 掉避免真实等待。
    monkeypatch.setattr(svc, "_wait_for_gateway_ready", lambda: None)

    health = svc.check_bot_health(auto_heal=True)
    # crash-loop 抑制应触发强制重启 gateway（端口可达也不应跳过）。
    assert restarted["n"] == 1
    assert health["crash_loop_recovered"] is True
    assert health["crash_loop_suppressed"] is True
    assert health["repaired"] is True
    # crash-loop 不是授权过期，不应误报 contextToken / 重扫。
    assert health["needs_user_message"] is False
    assert health["needs_relogin"] is False


def test_check_bot_health_crash_loop_suppressed_flag_only(monkeypatch):
    svc = _make_sender(monkeypatch)
    crash_payload = json.dumps({
        "channelAccounts": {
            "openclaw-weixin": [
                {
                    "accountId": "f4add82e8cf4-im-bot",
                    "id": "f4add82e8cf4-im-bot",
                    "lastError": "crash loop breaker tripped",
                    "running": False,
                }
            ]
        }
    })

    def fake_run(cmd, **kwargs):
        class P:
            returncode = 0
            stdout = crash_payload
            stderr = ""

        return P()

    monkeypatch.setattr(
        "src.notification_sender.openclaw_wechat_sender.subprocess.run", fake_run
    )
    # 仅探测（auto_heal=False）也应能暴露 crash_loop_suppressed 标志。
    health = svc.check_bot_health(auto_heal=False)
    assert health["crash_loop_suppressed"] is True


def test_split_text_by_bytes_keeps_multibyte_chars():
    """按字节切片不得切断多字节字符，且每片不超过上限。"""
    from src.notification_sender.openclaw_wechat_sender import OpenclawWechatSender

    text = "中文abc中文" * 500
    parts = OpenclawWechatSender._split_text_by_bytes(text, 10)
    assert "".join(parts) == text  # 不丢字符
    for part in parts:
        assert len(part.encode("utf-8")) <= 10


def test_send_text_long_content_split_into_multiple_messages(monkeypatch):
    """超长决策仪表盘按字节上限分多条发送、不截断，每条加 [i/N] 序号且不超过上限。"""
    from src.notification_sender.openclaw_wechat_sender import OPENCLAW_WECHAT_MAX_BYTES

    svc = _make_sender(monkeypatch)
    captured = []
    real_run = svc._run_cli

    def spy(extra_args, timeout_seconds=None):
        if "--message" in extra_args:
            captured.append(extra_args[extra_args.index("--message") + 1])
        return real_run(extra_args, timeout_seconds)

    monkeypatch.setattr(svc, "_run_cli", spy)

    # 约 15000 字节（中文每字 3 字节），远超单条 4000 上限。
    long_text = "决策仪表盘" * 1000
    assert svc.send_to_openclaw_wechat(long_text) is True
    assert len(captured) >= 2  # 已分多条

    n = len(captured)
    restored = ""
    for i, msg in enumerate(captured):
        assert len(msg.encode("utf-8")) <= OPENCLAW_WECHAT_MAX_BYTES
        assert msg.startswith(f"[{i + 1}/{n}] ")
        restored += msg.split("] ", 1)[1]
    assert restored == long_text  # 完整还原，无截断
