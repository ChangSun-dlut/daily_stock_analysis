# -*- coding: utf-8 -*-
"""板块流动分析（sector flow analysis）单元测试。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.services import screening_service


def _moneyflow_payload() -> dict:
    return {
        "available": True,
        "date": "2026-09-04",
        "source": "tushare",
        "total_inflow": 3.88e9,
        "total_outflow": -2.72e10,
        "summary_text": "主力净流入: 广告营销 +38.8亿；主力净流出: 电子 -272.1亿",
        "top_inflow": [
            {"name": "广告营销", "net_text": "+38.6亿", "top_stock_name": "蓝色光标"},
            {"name": "农牧饲渔", "net_text": "+30.2亿", "top_stock_name": "新希望"},
        ],
        "top_outflow": [
            {"name": "电子", "net_text": "-272.1亿", "top_stock_name": "寒武纪"},
        ],
    }


def _rotation_payload() -> dict:
    return {
        "top_buy": [{"name": "AI应用", "cum_change_5d": 8.2, "phase": "accelerating", "signal": "buy"}],
        "top_avoid": [],
    }


def _limit_up_rows() -> list:
    return [
        {
            "code": "600611",
            "name": "大众交通",
            "change_pct": 10.0,
            "first_limit_time": "092500",
            "consecutive_boards": 2,
            "break_count": 0,
            "industry": "智能驾驶",
        },
        {
            "code": "300059",
            "name": "东方财富",
            "change_pct": 20.0,
            "first_limit_time": "100300",
            "consecutive_boards": 1,
            "break_count": 1,
            "industry": "大金融",
        },
        {
            "code": "002230",
            "name": "科大讯飞",
            "change_pct": 10.0,
            "first_limit_time": "143500",
            "consecutive_boards": 1,
            "break_count": 0,
            "industry": "AI应用",
        },
    ]


def _patched_manager() -> MagicMock:
    manager = MagicMock()
    manager.get_limit_up_pool.return_value = _limit_up_rows()
    return manager


class TestExtractLlmJsonObject(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(screening_service._extract_llm_json_object('{"a": 1}'), {"a": 1})

    def test_fenced_json_with_prose(self):
        text = '前置说明\n```json\n{"summary": "ok"}\n```\n后缀'
        self.assertEqual(screening_service._extract_llm_json_object(text), {"summary": "ok"})

    def test_braces_inside_strings(self):
        text = '{"text": "包含花括号 { 与 } 的句子"}'
        self.assertIn("花括号", screening_service._extract_llm_json_object(text)["text"])

    def test_invalid(self):
        self.assertIsNone(screening_service._extract_llm_json_object("没有 JSON"))
        self.assertIsNone(screening_service._extract_llm_json_object('{"a": '))
        self.assertIsNone(screening_service._extract_llm_json_object(""))


class TestTimelineHelpers(unittest.TestCase):
    def test_parse_and_format_limit_time(self):
        self.assertEqual(screening_service._parse_limit_time_hhmmss("092500"), 92500)
        self.assertEqual(screening_service._parse_limit_time_hhmmss("92500"), 92500)
        self.assertEqual(screening_service._format_limit_time_hhmm("092500"), "09:25")
        self.assertEqual(screening_service._format_limit_time_hhmm("130605"), "13:06")
        self.assertEqual(screening_service._format_limit_time_hhmm(""), "")

    def test_phase_key_split(self):
        self.assertEqual(screening_service._limit_up_phase_key("092500"), "auction")
        self.assertEqual(screening_service._limit_up_phase_key("100300"), "morning")
        self.assertEqual(screening_service._limit_up_phase_key("130600"), "afternoon")
        self.assertEqual(screening_service._limit_up_phase_key("143500"), "late")


class TestBuildSectorFlowTimeline(unittest.TestCase):
    def test_phases_and_leaders(self):
        with patch.object(screening_service, "_get_dsa_fetcher_manager", return_value=_patched_manager()):
            timeline = screening_service._build_sector_flow_timeline(_moneyflow_payload())
        self.assertEqual(timeline["limit_up_count"], 3)
        by_key = {p["key"]: p for p in timeline["phases"]}
        self.assertEqual(by_key["auction"]["events"][0]["name"], "大众交通")
        self.assertEqual(by_key["morning"]["events"][0]["time"], "10:03")
        self.assertEqual(by_key["late"]["events"][0]["name"], "科大讯飞")
        leaders = timeline["leaders"]
        self.assertEqual(len(leaders), 3)
        self.assertEqual(leaders[0]["industry"], "智能驾驶")
        self.assertEqual(leaders[0]["name"], "大众交通")
        self.assertEqual(leaders[0]["boards"], 2)
        self.assertEqual(leaders[0]["time"], "09:25")

    def test_empty_pool(self):
        manager = MagicMock()
        manager.get_limit_up_pool.return_value = []
        with patch.object(screening_service, "_get_dsa_fetcher_manager", return_value=manager):
            timeline = screening_service._build_sector_flow_timeline(_moneyflow_payload())
        self.assertEqual(timeline["limit_up_count"], 0)
        self.assertEqual(timeline["leaders"], [])
        for phase in timeline["phases"]:
            self.assertEqual(phase["events"], [])


class TestFallbackAnalysis(unittest.TestCase):
    def test_fallback_phases_and_watch_points(self):
        with patch.object(screening_service, "_get_dsa_fetcher_manager", return_value=_patched_manager()):
            timeline = screening_service._build_sector_flow_timeline(_moneyflow_payload())
        result = screening_service._build_sector_flow_analysis_fallback(
            _moneyflow_payload(), _rotation_payload(), timeline
        )
        self.assertTrue(result["summary"])
        self.assertEqual(len(result["phases"]), 4)
        auction = result["phases"][0]
        self.assertEqual(auction["key"], "auction")
        self.assertIn("大众交通", auction["text"])
        self.assertTrue(auction["events"])
        self.assertIn("涨停", auction["events"][0]["text"])
        self.assertTrue(any("带头标的" in w for w in result["watch_points"]))

    def test_digest_contains_timeline_and_leaders(self):
        with patch.object(screening_service, "_get_dsa_fetcher_manager", return_value=_patched_manager()):
            timeline = screening_service._build_sector_flow_timeline(_moneyflow_payload())
        digest = screening_service._build_sector_flow_analysis_digest(
            _moneyflow_payload(), _rotation_payload(), timeline
        )
        self.assertIn("广告营销", digest)
        self.assertIn("带头标的", digest)
        self.assertIn("涨停时间线", digest)
        self.assertIn("09:25", digest)


class TestNormalizePhases(unittest.TestCase):
    def test_normalizes_llm_output_and_fills_missing(self):
        timeline = {
            "phases": [
                {
                    "key": "auction",
                    "events": [
                        {"time": "09:25", "name": "大众交通", "industry": "智能驾驶", "boards": 2}
                    ],
                }
            ]
        }
        parsed = {
            "phases": [
                {
                    "key": "auction",
                    "title": "竞价高开",
                    "time_range": "09:15 ~ 09:30",
                    "text": "竞价阶段智能驾驶抢跑。",
                    "events": [{"time": "09:25", "text": "智能驾驶 大众交通 一字涨停，2连板"}],
                },
                {"key": "fake_phase", "text": "不合法阶段"},
            ]
        }
        phases = screening_service._normalize_sector_flow_phases(parsed.get("phases"), timeline)
        self.assertEqual([p["key"] for p in phases], ["auction", "morning", "afternoon", "late"])
        auction = phases[0]
        self.assertEqual(auction["text"], "竞价阶段智能驾驶抢跑。")
        self.assertEqual(auction["events"][0]["text"], "智能驾驶 大众交通 一字涨停，2连板")
        morning = phases[1]
        self.assertIn("无明显涨停异动", morning["text"])
        self.assertEqual(morning["events"], [])

    def test_llm_missing_events_filled_from_timeline(self):
        timeline = {
            "phases": [
                {
                    "key": "morning",
                    "events": [
                        {"time": "10:03", "name": "东方财富", "industry": "大金融", "boards": 1}
                    ],
                }
            ]
        }
        phases = screening_service._normalize_sector_flow_phases([], timeline)
        morning = next(p for p in phases if p["key"] == "morning")
        self.assertEqual(morning["events"][0]["time"], "10:03")
        self.assertIn("大金融 东方财富 涨停", morning["events"][0]["text"])


class TestGetSectorFlowAnalysis(unittest.TestCase):
    def test_unavailable_moneyflow(self):
        with patch.object(screening_service, "get_sector_moneyflow", return_value={"available": False}):
            payload = screening_service.get_sector_flow_analysis(force_refresh=True)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["source"], "unavailable")

    def test_llm_success(self):
        llm_result = {
            "summary": "全天主力资金高低切换明显。",
            "phases": [
                {
                    "key": "auction",
                    "title": "竞价高开",
                    "time_range": "09:15 ~ 09:30",
                    "text": "竞价阶段智能驾驶抢跑，大众交通一字涨停。",
                    "events": [{"time": "09:25", "text": "智能驾驶 大众交通 一字涨停，2连板"}],
                }
            ],
            "watch_points": ["关注广告营销持续性"],
        }
        with patch.object(screening_service, "get_sector_moneyflow", return_value=_moneyflow_payload()), \
             patch.object(screening_service, "get_sector_rotation", return_value=_rotation_payload()), \
             patch.object(screening_service, "_get_dsa_fetcher_manager", return_value=_patched_manager()), \
             patch.object(screening_service, "_generate_sector_flow_analysis_with_llm", return_value=llm_result):
            payload = screening_service.get_sector_flow_analysis(force_refresh=True)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["source"], "llm")
        self.assertIn("高低切换", payload["summary"])
        self.assertEqual(payload["phases"][0]["key"], "auction")
        self.assertEqual(payload["phases"][0]["events"][0]["time"], "09:25")
        self.assertEqual(payload["inflow_names"], ["广告营销", "农牧饲渔"])
        self.assertEqual(payload["outflow_names"], ["电子"])
        self.assertEqual(payload["limit_up_count"], 3)
        self.assertTrue(payload["leaders"])

    def test_fallback_when_llm_fails(self):
        with patch.object(screening_service, "get_sector_moneyflow", return_value=_moneyflow_payload()), \
             patch.object(screening_service, "get_sector_rotation", return_value=_rotation_payload()), \
             patch.object(screening_service, "_get_dsa_fetcher_manager", return_value=_patched_manager()), \
             patch.object(screening_service, "_generate_sector_flow_analysis_with_llm", return_value=None), \
             patch.object(screening_service, "_write_sector_flow_analysis_cache"):
            payload = screening_service.get_sector_flow_analysis(force_refresh=True)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["source"], "fallback")
        self.assertTrue(payload["summary"])
        self.assertEqual(len(payload["phases"]), 4)
        self.assertTrue(payload["leaders"])


if __name__ == "__main__":
    unittest.main()
