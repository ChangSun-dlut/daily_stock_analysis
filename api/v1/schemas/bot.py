# -*- coding: utf-8 -*-
"""
===================================
Bot 命令相关响应模型
===================================

为 OpenClaw / ClawBot 技能暴露 DSA 指令清单。
"""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class BotCommandItem(BaseModel):
    """单条指令信息"""

    name: str = Field(..., description="指令主名")
    aliases: List[str] = Field(default_factory=list, description="指令别名")
    description: str = Field(..., description="指令说明")
    usage: str = Field(..., description="用法示例")
    category: str = Field(..., description="所属分组")
    admin_only: bool = Field(False, description="是否仅管理员可用")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "analyze",
                "aliases": ["a"],
                "description": "分析单只股票",
                "usage": "/analyze <股票代码> [报告类型]",
                "category": "行情分析",
                "admin_only": False,
            }
        }
    )


class BotCommandRequest(BaseModel):
    """下发命令请求"""

    command: str = Field(..., description="命令名，如 /screen、/analyze")
    args: List[str] = Field(default_factory=list, description="命令参数，如 --refresh")
    chat_id: str = Field(default="openclaw_wechat", description="对话 ID，用于结果推送回源")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "command": "/screen",
                "args": [],
                "chat_id": "openclaw_wechat",
            }
        }
    )


class BotCommandResponse(BaseModel):
    """命令执行响应"""

    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="响应文本")
    command: str = Field(..., description="执行的命令名")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "message": "✅ 横盘突破选股已启动\n\n正在扫描全市场 A 股，完成后通过微信推送结果（约 2–5 分钟）。",
                "command": "screen",
            }
        }
    )


class BotCommandsResponse(BaseModel):
    """指令清单响应"""

    total: int = Field(..., description="指令总数")
    markdown: str = Field(..., description="适合直接发送到微信/ClawBot 的 Markdown 文本")
    commands: List[BotCommandItem] = Field(default_factory=list, description="结构化指令列表")
    prefix: str = Field("/", description="指令前缀")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total": 11,
                "prefix": "/",
                "markdown": "📊 **DSA 已打通指令总览**\n...",
                "commands": [],
            }
        }
    )
