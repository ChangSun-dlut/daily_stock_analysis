# -*- coding: utf-8 -*-
"""
===================================
横盘突破选股命令
===================================

微信端触发「横盘突破」策略选股。若今日已选过，直接把缓存结果发回；
否则后台跑 AlphaSift 横盘突破筛选，完成后通过微信推送。

触发示例：
    /screen
    横盘选股
    横盘突破
    横盘选股 --refresh     # 强制重选
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse

logger = logging.getLogger(__name__)

STRATEGY = "sideways_breakout"
STRATEGY_LABEL = "横盘突破"


def _today_str() -> str:
    return datetime.now(timezone.utc).astimezone().date().isoformat()


class ScreenCommand(BotCommand):
    @property
    def name(self) -> str:
        return "screen"

    @property
    def aliases(self) -> List[str]:
        return ["横盘选股", "横盘突破", "横盘", "盘整", "sideways", "consolidation"]

    @property
    def description(self) -> str:
        return "横盘突破选股（今日）"

    @property
    def usage(self) -> str:
        return "/screen 或「横盘选股」（加 --refresh 强制重选）"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        force = any(a.lower() in ("--refresh", "-f", "刷新", "重选") for a in args)
        today = _today_str()

        cache = self._read_cache()
        if cache and cache.get("date") == today and not force:
            return BotResponse.markdown_response(self._format(cache))

        thread = threading.Thread(
            target=self._run_screen, args=(message, force, today), daemon=True
        )
        thread.start()
        return BotResponse.markdown_response(
            f"✅ **{STRATEGY_LABEL}选股已启动**\n\n"
            "正在扫描全市场 A 股，完成后通过微信推送结果（约 2–5 分钟）。"
        )

    def _read_cache(self) -> Optional[Dict[str, Any]]:
        try:
            from src.services.screening_service import read_alphasift_screen_cache

            return read_alphasift_screen_cache(STRATEGY)
        except Exception as exc:
            logger.warning("[ScreenCommand] 读取选股缓存失败: %s", exc)
            return None

    def _format(self, cache: Dict[str, Any]) -> str:
        date_str = cache.get("date", "")
        candidates: List[Dict[str, Any]] = cache.get("candidates") or []
        header = f"📊 **{STRATEGY_LABEL}选股 · {date_str}**\n"
        if not candidates:
            return header + "\n今日未选出符合条件的个股。"

        lines: List[str] = []
        for idx, c in enumerate(candidates[:30], start=1):
            rank = c.get("rank") or idx
            name = c.get("name", "")
            code = c.get("code", "")
            price = c.get("price")
            chg = c.get("change_pct")
            score = c.get("final_score")

            parts = [f"{rank}. {name}({code})"]
            if price is not None and price != -1:
                parts.append(f"¥{price}")
            if chg is not None:
                parts.append(f"{chg:+.2f}%")
            if score is not None:
                parts.append(f"分{score:.1f}")
            line = " ".join(parts)
            reason = (c.get("ranking_reason") or "").strip()
            if reason:
                line += f"\n   理由: {reason[:80]}"
            lines.append(line)

        body = "\n".join(lines)
        return header + "\n" + body + f"\n\n共 {len(candidates)} 只。"

    def _run_screen(self, message: BotMessage, force: bool, today: str) -> None:
        try:
            if not force:
                cache = self._read_cache()
                if cache and cache.get("date") == today:
                    _push_to_source(self._format(cache), message)
                    return

            from src.config import get_config
            from src.services.screening_service import AlphaSiftService

            config = get_config()
            service = AlphaSiftService(config)
            result = service.screen(strategy=STRATEGY, market="cn", max_results=20)

            # service.screen 已写入当日缓存
            _push_to_source(self._format(result), message)
        except Exception as exc:
            logger.error("[ScreenCommand] 选股失败: %s", exc, exc_info=True)
            try:
                _push_to_source(
                    f"⚠️ {STRATEGY_LABEL}选股失败：{exc}", message
                )
            except Exception:
                pass


def _push_to_source(text: str, message: BotMessage) -> None:
    """按来源通道（微信）推送文本。"""
    from src.notification import NotificationService

    NotificationService(source_message=message).send(text, route_type="report")
