# -*- coding: utf-8 -*-
"""微信(OpenClaw) 通道健康巡检后台任务：心跳检测 + 自动拉起。

背景（2026-08-31 修复）：

- 该任务原先内联在 ``main.py`` 的「CLI 定时任务模式」分支里。而生产实际以
  ``--webui --schedule`` 运行，走的是 **Web/API runtime scheduler** 分支，该分支
  在 ``main.py`` 中直接 ``sleep`` + ``return``（原 1603-1608 行），**后台任务根本
  注册不进去** —— 通道挂掉后无人自愈，表现为「微信推送静默中断」。
- 现把构建逻辑抽到本模块，由 ``RuntimeSchedulerService`` 与 CLI 定时任务模式**共用**，
  避免两份实现漂移。
- 同时修掉原实现的缩进 bug：上下文续期提醒那段（原 main.py 1741-1786 行）写在
  ``openclaw_health_task`` 函数体**之外**，却引用了函数内的局部变量
  （``needs_user_message`` / ``health`` / ``sender``），一旦配置了微信通道，启动
  定时任务模式时就会抛 ``NameError``。生产因为走 Web 分支提前 return 未触发。

巡检动作（每轮）：

1. ``check_bot_health(auto_heal=True)``：gateway 失活自动拉起、contextToken 失效
   自动清 token + 重启、crash-loop breaker 强制重启。
2. **心跳兜底**：通道 ``running=false``（gateway 端口可达但通道被 breaker 抑制）
   时用 RPC ``channels.start`` 直接拉起 —— 比重启 gateway 安全，不制造 unclean
   boot，也不会再次触发 breaker。
3. 分级告警 + 限频；上下文临近到期时提醒接收人发消息续期（失败转网页弹窗）。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

# 巡检间隔（秒）。
DEFAULT_INTERVAL_SECONDS = 30 * 60
# 「contextToken 已重置，等待用户发消息」告警的限频窗口（秒）。
USER_MSG_ALERT_COOLDOWN_SECONDS = 6 * 3600
TASK_NAME = "openclaw_wechat_health"


def build_openclaw_wechat_health_background_tasks(
    config: Any,
    *,
    config_provider: Callable[[], Any],
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> List[Dict[str, Any]]:
    """构建微信通道巡检后台任务。

    Args:
        config: 当前配置快照（用于判断是否启用该通道）。
        config_provider: 每轮执行时重新加载配置的回调。
        interval_seconds: 巡检间隔，默认 30 分钟。

    Returns:
        scheduler 后台任务条目列表；未配置微信通道时返回空列表。
    """
    if not (
        getattr(config, "openclaw_wechat_account", None)
        and getattr(config, "openclaw_wechat_target", None)
    ):
        return []

    from src.notification_sender.openclaw_wechat_sender import (
        OPENCLAW_ERR_AUTH_NEEDED,
        OPENCLAW_ERR_CDN,
        OPENCLAW_ERR_GATEWAY,
        OpenclawWechatSender,
    )

    # 限频：contextToken 已自动重置、仅等待用户发消息的情形，避免每 30 分钟重复告警。
    _openclaw_last_user_msg_alert_ts = {"ts": 0.0}
    # 限频：上下文临近到期时的「请发消息续期」提醒，避免周期内重复打扰。
    _openclaw_last_keepalive_ts = {"ts": 0.0}
    _openclaw_last_context_mtime = {"mt": 0.0}

    def openclaw_health_task() -> None:
        sender = OpenclawWechatSender(config_provider())
        # auto_heal=True：gateway 失活自动拉起；contextToken 失效自动清 token + 重启。
        health = sender.check_bot_health(
            restart_gateway_if_down=True, auto_heal=True
        )
        if not health.get("enabled"):
            return
        needs_user_message = bool(health.get("needs_user_message"))
        err_type = health.get("last_send_error_type")
        persistent = err_type in (
            OPENCLAW_ERR_AUTH_NEEDED,
            OPENCLAW_ERR_CDN,
            OPENCLAW_ERR_GATEWAY,
        )

        # 心跳兜底：gateway 端口可达 ≠ 通道在跑。crash-loop breaker 跳闸时端口正常
        # 但通道被禁止自启，此时推送静默失败 —— 用 RPC 直接拉起通道。
        if not needs_user_message and not health.get("crash_loop_recovered"):
            try:
                if sender.channel_running() is False:
                    logger.warning(
                        "[OpenClawHealth] 微信通道未运行（可能被 crash-loop breaker 抑制），"
                        "尝试 RPC channels.start 拉起"
                    )
                    sender.ensure_channel_started()
            except Exception as exc:  # noqa: BLE001 - 巡检任务不能因兜底失败中断
                logger.warning("[OpenClawHealth] 通道状态检查/拉起失败: %s", exc)

        if needs_user_message:
            # contextToken 已自动重置，只需接收人发一条消息即可恢复；
            # 告警降级为 warning 并限频，避免反复打扰。
            now = time.time()
            if now - _openclaw_last_user_msg_alert_ts["ts"] < USER_MSG_ALERT_COOLDOWN_SECONDS:
                logger.debug(
                    "[OpenClawHealth] 微信通道 contextToken 已重置，等待用户发消息: %s",
                    health.get("detail"),
                )
                return
            _openclaw_last_user_msg_alert_ts["ts"] = now
            cleared = (
                "已自动清token+重启gateway"
                if health.get("context_token_cleared")
                else "需人工"
            )
            logger.warning(
                "[OpenClawHealth] 微信通道 contextToken 失效，%s；"
                "请给 bot 发一条任意微信消息以重建对话上下文，之后即可自动推送。"
                " detail=%s",
                cleared,
                health.get("detail"),
            )
        elif health.get("needs_relogin") or persistent:
            logger.error(
                "[OpenClawHealth] 微信通道异常: %s | "
                "需用户介入重新扫码登录（openclaw channels login --channel openclaw-weixin）。"
                " last_send_error_type=%s last_send_error_detail=%s",
                health.get("detail"),
                health.get("last_send_error_type"),
                health.get("last_send_error_detail"),
            )
        elif health.get("crash_loop_recovered"):
            # gateway 曾因 crash-loop breaker 暂停通道，本次巡检已强制重启清除。
            # 已自动恢复，记录一条 error 级便于追溯，但无需用户介入。
            logger.error(
                "[OpenClawHealth] 微信通道 gateway 曾因 crash-loop breaker 被抑制，"
                "已自动强制重启清除并恢复通道。 detail=%s",
                health.get("detail"),
            )
        elif health.get("crash_loop_suppressed"):
            # 处于抑制状态但冷却期内未重启，记录 warning 便于观察。
            logger.warning(
                "[OpenClawHealth] 微信通道 gateway 处于 crash-loop 抑制状态"
                "(冷却期内暂不强制重启): %s",
                health.get("detail"),
            )
        else:
            logger.debug(
                "[OpenClawHealth] 微信通道健康: %s",
                health.get("detail"),
            )

        # 上下文临近到期：趁仍能推送，主动提醒接收人发消息续期。
        # 防止过期后 bot 无法主动推送、只能等用户在微信里发消息。
        # 注意：这段必须在函数体内（原实现误放在函数外，会抛 NameError）。
        if not needs_user_message and health.get("gateway_reachable"):
            now = time.time()
            keepalive_ok, _keepalive_reason = sender.should_send_context_keepalive()
            if keepalive_ok:
                mt = sender.context_token_mtime() or 0.0
                if mt > _openclaw_last_context_mtime["mt"] + 60:
                    # 接收人刚发消息重建了上下文 → 重置本周期提醒计时。
                    _openclaw_last_context_mtime["mt"] = mt
                    _openclaw_last_keepalive_ts["ts"] = 0.0
                keepalive_gap = float(
                    os.environ.get("OPENCLAW_CONTEXT_TTL_SECONDS", 72 * 3600)
                )
                if now - _openclaw_last_keepalive_ts["ts"] >= keepalive_gap:
                    _openclaw_last_keepalive_ts["ts"] = now
                    remind_text = (
                        "⏰ 微信推送续期提醒：OpenClaw 对话上下文即将到期，"
                        "到期后将无法自动推送预警。请给本 bot 发一条任意微信消息"
                        "以重建对话上下文，保持推送不中断。"
                    )
                    sent = sender.send_to_openclaw_wechat(
                        remind_text, auto_repair=False
                    )
                    if not sent:
                        # 微信已不可用（上下文其实已失效）：转网页弹窗兜底提醒。
                        try:
                            from src.services.web_alert_hub import get_web_alert_hub

                            get_web_alert_hub().push(
                                "微信推送即将失效",
                                "OpenClaw 对话上下文即将到期，请给 bot 发一条微信消息以续期，"
                                "否则预警推送会中断。",
                                level="warning",
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        logger.warning(
                            "[OpenClawHealth] 上下文续期提醒发送失败（微信已不可用），"
                            "已转网页弹窗提醒。"
                        )
                    else:
                        logger.info(
                            "[OpenClawHealth] 已发送微信续期提醒（上下文临近到期）"
                        )

    return [{
        "task": openclaw_health_task,
        "interval_seconds": interval_seconds,
        "run_immediately": True,
        "name": TASK_NAME,
    }]


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "TASK_NAME",
    "build_openclaw_wechat_health_background_tasks",
]
