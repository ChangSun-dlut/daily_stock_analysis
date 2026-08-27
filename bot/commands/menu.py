# -*- coding: utf-8 -*-
"""
===================================
功能菜单命令
===================================

展示 DSA 当前已打通（已接入调度器）的全部指令，并支持把该菜单
通过 OpenClaw 推送到微信，便于在微信里查看指令总览。

用法：
    /menu               - 在当前会话显示指令总览
    /menu --wechat      - 同时把指令总览推送到微信（OpenClaw）
    /menu -w            - 同上
"""

from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse


# 指令分组（按命令名归类；未列出的归入「其他」）
_CATEGORY_MAP = {
    "analyze": "行情分析",
    "batch": "行情分析",
    "research": "行情分析",
    "market": "行情选股",
    "history": "行情选股",
    "strategies": "行情选股",
    "ask": "智能问答",
    "chat": "智能问答",
    "status": "系统",
    "help": "系统",
    "menu": "系统",
}


class MenuCommand(BotCommand):
    """
    功能菜单命令

    列出当前调度器里所有已注册、可用的指令，并可在微信中展示。
    """

    @property
    def name(self) -> str:
        return "menu"

    @property
    def aliases(self) -> List[str]:
        return ["指令", "功能", "菜单", "dsa", "commands"]

    @property
    def description(self) -> str:
        return "DSA 功能菜单（所有已打通指令）"

    @property
    def usage(self) -> str:
        return "/menu [--wechat|-w]"

    @property
    def admin_only(self) -> bool:
        return False

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        # 延迟导入避免循环依赖
        from bot.dispatcher import get_dispatcher

        dispatcher = get_dispatcher()
        commands = dispatcher.list_commands(include_hidden=False)
        prefix = dispatcher.command_prefix

        catalog = self._build_catalog(commands, prefix)

        # 是否同步推送到微信
        push_wechat = any(a in ("--wechat", "-w") for a in args)
        wechat_note = ""
        if push_wechat:
            wechat_note = self._push_to_wechat(catalog)

        reply = catalog
        if wechat_note:
            reply += "\n\n" + wechat_note

        return BotResponse.markdown_response(reply)

    # ------------------------------------------------------------------
    # 目录构建
    # ------------------------------------------------------------------
    def _build_catalog(self, commands: List[BotCommand], prefix: str) -> str:
        """按分组构建指令总览文本。"""
        grouped = {}
        for cmd in commands:
            cat = _CATEGORY_MAP.get(cmd.name, "其他")
            grouped.setdefault(cat, []).append(cmd)

        # 固定分组顺序
        order = ["行情分析", "行情选股", "智能问答", "系统", "其他"]
        cats = [c for c in order if c in grouped] + [
            c for c in grouped if c not in order
        ]

        lines = [
            "📊 **DSA 已打通指令总览**",
            f"共 {len(commands)} 条可用指令",
            "",
        ]

        for cat in cats:
            lines.append(f"【{cat}】")
            for cmd in grouped[cat]:
                aliases = [
                    a for a in cmd.aliases if a.isascii() and a not in ("h",)
                ]
                alias_str = ""
                if aliases:
                    alias_str = "  (" + ", ".join(prefix + a for a in aliases[:3]) + ")"
                admin_tag = " `🔒管理员`" if cmd.admin_only else ""
                lines.append(
                    f"• {prefix}{cmd.name}{alias_str} - {cmd.description}{admin_tag}"
                )
            lines.append("")

        lines.extend([
            "---",
            f"💡 输入 {prefix}help <命令名> 查看详细用法",
            f"📲 加 `--wechat` 可把本菜单推送到微信",
        ])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 微信推送
    # ------------------------------------------------------------------
    def _push_to_wechat(self, catalog: str) -> str:
        """把菜单文本推送到微信（OpenClaw）。返回状态说明。"""
        try:
            from src.notification import NotificationService

            notifier = NotificationService(source_message=None)
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ 微信推送初始化失败：{exc}"

        try:
            ok = notifier.send_to_openclaw_wechat(catalog)
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ 微信推送失败：{exc}"

        if ok:
            return "✅ 已同步推送到微信（OpenClaw）"
        return "⚠️ 微信未配置或未推送成功（检查 OPENCLAW_WECHAT 账号/接收人）"
