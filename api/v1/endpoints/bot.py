# -*- coding: utf-8 -*-
"""
===================================
Bot 命令接口
===================================

为 OpenClaw / ClawBot 等外部入口暴露 DSA 指令清单，
使外部 Skill 能够返回当前已打通的全部 DSA 命令。
"""

import logging
import time
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_config_dep
from api.v1.schemas.bot import BotCommandItem, BotCommandRequest, BotCommandResponse, BotCommandsResponse
from bot.dispatcher import get_dispatcher
from bot.models import BotMessage, ChatType
from src.config import Config

logger = logging.getLogger(__name__)

router = APIRouter()

# 命令分组（按命令名归类；未列出的归入「其他」）
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


def _build_catalog(commands: List[BotCommandItem], prefix: str) -> str:
    """按分组构建适合微信/ClawBot 展示的 Markdown 目录。"""
    grouped = {}
    for cmd in commands:
        grouped.setdefault(cmd.category, []).append(cmd)

    order = ["行情分析", "行情选股", "智能问答", "系统", "其他"]
    cats = [c for c in order if c in grouped] + [c for c in grouped if c not in order]

    lines = [
        "📊 **DSA 已打通指令总览**",
        f"共 {len(commands)} 条可用指令",
        "",
    ]
    for cat in cats:
        lines.append(f"【{cat}】")
        for cmd in grouped[cat]:
            alias_line = ""
            ascii_aliases = [a for a in cmd.aliases if a.isascii()]
            if ascii_aliases:
                alias_line = "  (" + ", ".join(prefix + a for a in ascii_aliases[:3]) + ")"
            admin_tag = " `🔒管理员`" if cmd.admin_only else ""
            lines.append(
                f"• {prefix}{cmd.name}{alias_line} - {cmd.description}{admin_tag}"
            )
        lines.append("")

    lines.extend([
        "---",
        f"💡 输入 {prefix}help <命令名> 查看详细用法",
    ])
    return "\n".join(lines)


@router.get(
    "/commands",
    response_model=BotCommandsResponse,
    summary="获取 DSA 指令清单",
    description="返回当前已注册的全部 DSA 指令，包含结构化列表和可直接发送到微信/ClawBot 的 Markdown 文本。",
    tags=["Bot"],
)
async def list_bot_commands(
    config: Config = Depends(get_config_dep),
) -> BotCommandsResponse:
    """列出所有已注册 DSA 指令。"""
    dispatcher = get_dispatcher()
    prefix = dispatcher.command_prefix
    raw_commands = dispatcher.list_commands(include_hidden=False)

    commands: List[BotCommandItem] = []
    for cmd in raw_commands:
        commands.append(
            BotCommandItem(
                name=cmd.name,
                aliases=cmd.aliases,
                description=cmd.description,
                usage=cmd.usage,
                category=_CATEGORY_MAP.get(cmd.name, "其他"),
                admin_only=cmd.admin_only,
            )
        )

    markdown = _build_catalog(commands, prefix)

    return BotCommandsResponse(
        total=len(commands),
        prefix=prefix,
        markdown=markdown,
        commands=commands,
    )


@router.post(
    "/{chat_id}/command",
    response_model=BotCommandResponse,
    summary="下发命令给 DSA Bot",
    description="向指定会话下发 DSA 命令（如 /screen、/analyze），由 Bot Dispatcher 执行并返回结果。",
    tags=["Bot"],
)
async def post_command(
    chat_id: str,
    req: BotCommandRequest,
    config: Config = Depends(get_config_dep),
) -> BotCommandResponse:
    """通过 OpenClaw Agent 下发命令，用于 SKILL.md 中的 convention 端点。"""
    dispatcher = get_dispatcher()
    prefix = dispatcher.command_prefix

    # 从请求中提取命令名（去掉前缀，支持 /screen 或 screen 两种写法）
    raw_cmd = req.command.strip()
    cmd_name = raw_cmd
    if cmd_name.startswith(prefix):
        cmd_name = cmd_name[len(prefix):]

    # 构造 BotMessage
    content = f"{prefix}{cmd_name}"
    if req.args:
        content += " " + " ".join(req.args)

    message = BotMessage(
        platform="openclaw",
        message_id=f"api_{chat_id}_{int(time.time())}",
        user_id=chat_id,
        user_name=chat_id,
        chat_id=chat_id,
        chat_type=ChatType.PRIVATE,
        content=content,
        raw_content=content,
        mentioned=False,
    )

    # 执行命令
    try:
        response = dispatcher.dispatch(message)
        return BotCommandResponse(
            success=True,
            message=response.text,
            command=cmd_name,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Bot API] 命令 {cmd_name} 执行失败: {e}", exc_info=True)
        return BotCommandResponse(
            success=False,
            message=f"命令执行失败: {str(e)[:200]}",
            command=cmd_name,
        )
