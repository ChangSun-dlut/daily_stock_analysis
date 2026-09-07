# -*- coding: utf-8 -*-
"""
===================================
大盘复盘命令
===================================

执行大盘复盘分析，生成市场概览报告。
"""

import logging
import threading
from typing import Any, List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse

logger = logging.getLogger(__name__)


class MarketCommand(BotCommand):
    """
    大盘复盘命令

    执行大盘复盘分析，包括：
    - 主要指数表现
    - 板块热点
    - 市场情绪
    - 后市展望

    用法：
        /market - 执行大盘复盘
    """

    @property
    def name(self) -> str:
        return "market"

    @property
    def aliases(self) -> List[str]:
        return ["m", "大盘", "复盘", "行情"]

    @property
    def description(self) -> str:
        return "大盘复盘分析"

    @property
    def usage(self) -> str:
        return "/market"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行大盘复盘命令"""
        config = self._get_config()
        lock_token = self._try_acquire_market_review_lock(config)
        if lock_token is None:
            return BotResponse.markdown_response("⚠️ 大盘复盘正在执行中，请稍后再试。")

        thread = threading.Thread(
            target=self._run_market_review,
            args=(message, config, lock_token),
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            logger.error(
                "[MarketCommand] 大盘复盘后台线程启动失败: %s",
                exc,
            )
            self._release_market_review_lock(lock_token)
            return BotResponse.error_response(
                "大盘复盘启动失败，已释放运行锁；请稍后重试"
            )

        return BotResponse.markdown_response(
            "✅ **大盘复盘任务已启动**\n\n"
            "正在分析：\n"
            "• 主要指数表现\n"
            "• 板块热点分析\n"
            "• 市场情绪判断\n"
            "• 后市展望\n\n"
            "分析完成后将自动推送结果。"
        )

    def _get_config(self):
        from src.config import get_config
        return get_config()

    def _try_acquire_market_review_lock(self, config):
        from src.core.market_review_lock import try_acquire_market_review_lock
        return try_acquire_market_review_lock(config)

    def _release_market_review_lock(self, lock_token: Optional[Any]) -> None:
        from src.core.market_review_lock import release_market_review_lock
        release_market_review_lock(lock_token)

    def _compute_market_review_override_region(self, config) -> Optional[str]:
        if not getattr(config, "trading_day_check_enabled", True):
            return None

        try:
            from src.core.trading_calendar import (
                get_open_markets_today,
                compute_effective_region,
            )

            open_markets = get_open_markets_today()
            return compute_effective_region(
                getattr(config, "market_review_region", "cn") or "cn",
                open_markets,
            )
        except Exception as exc:
            logger.warning("交易日过滤失败，按配置继续执行大盘复盘: %s", exc)
            return None

    def _send_existing_market_review(
        self,
        *,
        config,
        notifier,
        analyzer,
        search_service,
        override_region: Optional[str],
    ) -> bool:
        """当天已生成过大盘复盘时直接复用并推送，避免重复调用 LLM。

        与 main.py ``run_full_analysis`` 的 ``can_skip_market_review`` 语义一致：
        只读取已有上下文（``allow_generate=False``），不触发生成。

        Returns:
            True 表示已复用并推送（调用方应直接返回，不再重新生成）。
        """
        try:
            from src.services.daily_market_context import DailyMarketContextService

            region = (
                override_region
                or getattr(config, "market_review_region", "cn")
                or "cn"
            )
            context = DailyMarketContextService().get_context(
                region=region,
                config=config,
                notifier=notifier,
                analyzer=analyzer,
                search_service=search_service,
                allow_generate=False,
                persist_market_review_history=False,
            )
        except Exception as exc:
            logger.warning(
                "[MarketCommand] 读取当日大盘上下文失败，改为重新生成: %s", exc
            )
            return False

        if context is None:
            return False

        # 只复用明确来源（库里已存的历史复盘 / 本轮运行时生成）的内容，
        # 与 main.py _prime_daily_market_context 的过滤条件保持一致。
        if getattr(context, "source", None) not in (
            "analysis_history",
            "market_review_runtime",
        ):
            return False

        report = (
            (getattr(context, "full_report", "") or "").strip()
            or (getattr(context, "summary", "") or "").strip()
        )
        if not report:
            return False

        logger.info(
            "[MarketCommand] 复用当日已有的大盘复盘并推送: source=%s trade_date=%s",
            getattr(context, "source", "-"),
            getattr(context, "trade_date", "-"),
        )
        if notifier.is_available():
            if notifier.send(
                f"# 🎯 大盘复盘\n\n{report}",
                email_send_to_all=True,
                route_type="report",
            ):
                logger.info("[MarketCommand] 复用大盘复盘推送成功")
            else:
                logger.warning("[MarketCommand] 复用大盘复盘推送失败")
        return True

    def _run_market_review(
        self,
        message: BotMessage,
        config,
        lock_token: Optional[Any],
    ) -> None:
        """后台执行大盘复盘"""
        try:
            override_region = self._compute_market_review_override_region(config)
            if override_region == "":
                from src.notification import NotificationService
                notifier = NotificationService(source_message=message)
                logger.info("[MarketCommand] 今日相关市场休市，跳过大盘复盘")
                if notifier.is_available():
                    notifier.send(
                        "🎯 大盘复盘\n\n今日相关市场休市，已跳过大盘复盘。",
                        email_send_to_all=True,
                        route_type="report",
                    )
                return

            from src.core.market_review_runtime import build_market_review_runtime
            from src.core.market_review import run_market_review

            notifier, analyzer, search_service = build_market_review_runtime(
                config,
                source_message=message,
            )
            # 当天已生成过大盘复盘时直接复用推送，不再重复调用 LLM。
            if self._send_existing_market_review(
                config=config,
                notifier=notifier,
                analyzer=analyzer,
                search_service=search_service,
                override_region=override_region,
            ):
                return

            review_report = run_market_review(
                notifier=notifier,
                analyzer=analyzer,
                search_service=search_service,
                send_notification=True,
                override_region=override_region,
                trigger_source="bot",
            )
            if review_report:
                logger.info("[MarketCommand] 大盘复盘完成并已推送")
            else:
                logger.warning("[MarketCommand] 大盘复盘返回空结果")
        except Exception as e:
            logger.error("[MarketCommand] 大盘复盘失败: %s", e)
            logger.exception(e)
        finally:
            self._release_market_review_lock(lock_token)
