# -*- coding: utf-8 -*-
"""自动注册「同比昨日同期放量」分钟级告警规则。

需求背景：所有自选股 + 选股选出来的票，每天 09:00-10:00 期间做分钟级的
「同比昨天放量」监控（今日累计成交量 vs 昨日同一时段累计成交量）。

两类覆盖方式：

1. **自选股**：建一条 ``target_scope=watchlist`` 的规则。评估时由
   :func:`expand_symbol_targets` 展开成当前自选股列表，因此用户后续新增自选股
   会自动纳入监控，无需重建规则。
2. **选股结果**：由 ``screening_service`` 在每轮选股后按股票逐条注册
   ``single_symbol`` 规则，按 ``(source, alert_type, target)`` 幂等去重。

所有注册入口都是幂等的：已存在则更新参数并重新启用，不存在才新建。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VOLUME_YOY_ALERT_TYPE = "volume_yoy_surge_rt"

# 独立 source 便于区分规则来源、也作为幂等键的一部分。
WATCHLIST_ALERT_SOURCE = "watchlist_yoy"
SCREENING_ALERT_SOURCE = "consolidation_breakout_yoy"

# 默认参数：同比昨日同期放大 ≥2x 触发（3x 强 / 5x 极强），监控时段 09:00-10:00。
DEFAULT_VOLUME_YOY_PARAMETERS: Dict[str, Any] = {
    "min_multiplier": 2.0,
    "strong_multiplier": 3.0,
    "extreme_multiplier": 5.0,
    "session_start": "09:00",
    "session_end": "10:00",
}

# 30 分钟的监控窗口内，同一只股票默认最多提醒两次（15 分钟冷却）。
# 注意：全局 ALERT_REALTIME_PUSH=true 时会绕过冷却（每轮都推），这是既有行为。
VOLUME_YOY_COOLDOWN_SECONDS = 900

_WATCHLIST_TARGET = "default"


def _alert_service() -> Any:
    from src.services.alert_service import AlertService

    return AlertService()


def _cooldown_policy() -> Dict[str, Any]:
    return {"cooldown_seconds": VOLUME_YOY_COOLDOWN_SECONDS}


def register_watchlist_volume_yoy_alert(
    *,
    parameters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """确保「所有自选股」都有一条同比昨日放量规则（幂等）。

    返回 ``{"created": bool, "updated": bool, "rule_id": int | None, "total_targets": int | None}``。
    """
    service = _alert_service()
    params = dict(DEFAULT_VOLUME_YOY_PARAMETERS)
    if parameters:
        params.update(parameters)

    result: Dict[str, Any] = {
        "created": False,
        "updated": False,
        "rule_id": None,
        "target_count": None,
    }
    try:
        existing = service.list_rules(
            alert_type=VOLUME_YOY_ALERT_TYPE,
            source=WATCHLIST_ALERT_SOURCE,
            page_size=10,
        )
        name = "自选股·早盘同比昨日放量预警"
        payload = {
            "target_scope": "watchlist",
            "target": _WATCHLIST_TARGET,
            "alert_type": VOLUME_YOY_ALERT_TYPE,
            "parameters": params,
            "name": name,
            "severity": "warning",
            "enabled": True,
            "cooldown_policy": _cooldown_policy(),
        }
        total = int(existing.get("total") or 0)
        if total:
            item = existing["items"][0]
            service.update_rule(
                int(item["id"]),
                {
                    "enabled": True,
                    "parameters": params,
                    "cooldown_policy": _cooldown_policy(),
                    "name": name,
                },
            )
            result.update({"updated": True, "rule_id": int(item["id"])})
        else:
            fields = service._normalize_rule_payload(
                payload, source=WATCHLIST_ALERT_SOURCE
            )
            row = service.repo.create_rule(fields)
            result.update(
                {"created": True, "rule_id": int(getattr(row, "id", 0) or 0) or None}
            )
        # 上报当前展开的自选股数量，便于确认覆盖规模（受软上限 100 约束）。
        try:
            row = service.repo.get_rule(int(result["rule_id"]))
            payloads = service.build_runtime_payloads(row)
            codes = {
                str(getattr(p.rule, "stock_code", "") or "")
                for p in payloads
                if getattr(p.rule, "stock_code", None)
            }
            result["target_count"] = len([c for c in codes if c])
        except Exception:  # pragma: no cover - best effort statistics only
            pass
    except Exception as exc:  # pragma: no cover - never break startup/screening
        logger.warning("[同比放量] 自选股规则注册失败: %s", exc)
        result["error"] = str(exc)
    return result


def register_volume_yoy_alerts_for_codes(
    codes: List[str], *, parameters: Optional[Dict[str, Any]] = None
) -> Dict[str, int]:
    """给选股出来的票批量注册同比昨日放量规则（幂等）。

    已存在同 ``(source, alert_type, target)`` 规则则更新参数并重新启用。
    """
    from data_provider.base import normalize_stock_code

    stats: Dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    if not codes:
        return stats

    params = dict(DEFAULT_VOLUME_YOY_PARAMETERS)
    if parameters:
        params.update(parameters)

    service = _alert_service()
    for raw in codes:
        norm = normalize_stock_code(str(raw)) if raw else ""
        if not norm:
            stats["skipped"] += 1
            continue
        name = f"选股·早盘同比昨日放量预警 {norm}"
        try:
            existing = service.list_rules(
                alert_type=VOLUME_YOY_ALERT_TYPE,
                target=norm,
                source=SCREENING_ALERT_SOURCE,
                page_size=5,
            )
            if int(existing.get("total") or 0):
                service.update_rule(
                    int(existing["items"][0]["id"]),
                    {
                        "enabled": True,
                        "parameters": params,
                        "cooldown_policy": _cooldown_policy(),
                        "name": name,
                    },
                )
                stats["updated"] += 1
            else:
                fields = service._normalize_rule_payload(
                    {
                        "target_scope": "single_symbol",
                        "target": norm,
                        "alert_type": VOLUME_YOY_ALERT_TYPE,
                        "parameters": params,
                        "name": name,
                        "severity": "warning",
                        "enabled": True,
                        "cooldown_policy": _cooldown_policy(),
                    },
                    source=SCREENING_ALERT_SOURCE,
                )
                service.repo.create_rule(fields)
                stats["created"] += 1
        except Exception as exc:  # defensive: never break screening flow
            logger.warning("[同比放量] 注册失败 %s: %s", norm, exc)
            stats["failed"] += 1
    return stats


__all__ = [
    "VOLUME_YOY_ALERT_TYPE",
    "register_volume_yoy_alerts_for_codes",
    "register_watchlist_volume_yoy_alert",
]
