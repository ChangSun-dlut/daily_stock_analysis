# -*- coding: utf-8 -*-
"""大盘复盘「明日交易计划」明日作战地图提示词回归测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.market_analyzer import MarketAnalyzer


def _analyzer(*, has_market_stats: bool, has_sector_rankings: bool) -> MarketAnalyzer:
    with patch.object(MarketAnalyzer, "__init__", lambda self: None):
        analyzer = MarketAnalyzer()
    analyzer.profile = SimpleNamespace(
        has_market_stats=has_market_stats,
        has_sector_rankings=has_sector_rankings,
    )
    return analyzer


class TestStrategyPlanBattleMapPrompt(unittest.TestCase):
    def test_zh_full_template_contains_battle_map(self):
        analyzer = _analyzer(has_market_stats=True, has_sector_rankings=True)
        template = analyzer._build_output_template_sections("zh")
        self.assertIn("### 六、明日交易计划", template)
        self.assertIn("明日作战地图", template)
        self.assertIn("| 时间/触发 | 观察点 | 应对动作 |", template)
        for row in ("竞价", "量能", "关键点位", "盘中情绪监控", "重要事件", "仓位纪律"):
            self.assertIn(row, template)
        self.assertIn("核心矛盾一句话", template)
        # 保持 share_image 海报解析兼容的带标签字段
        for label in ("结论", "仓位区间", "关注方向", "回避方向", "触发失效条件"):
            self.assertIn(label, template)

    def test_zh_fallback_template_contains_battle_map(self):
        analyzer = _analyzer(has_market_stats=False, has_sector_rankings=False)
        template = analyzer._build_output_template_sections("zh")
        self.assertIn("明日作战地图", template)
        self.assertIn("核心矛盾一句话", template)

    def test_en_full_template_contains_battle_map(self):
        analyzer = _analyzer(has_market_stats=True, has_sector_rankings=True)
        template = analyzer._build_output_template_sections("en")
        self.assertIn("Battle Map", template)
        self.assertIn("Core tension in one sentence", template)

    def test_en_fallback_template_contains_battle_map(self):
        analyzer = _analyzer(has_market_stats=False, has_sector_rankings=False)
        template = analyzer._build_output_template_sections("en")
        self.assertIn("Battle Map", template)
        self.assertIn("Core tension in one sentence", template)


if __name__ == "__main__":
    unittest.main()
