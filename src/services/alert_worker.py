# -*- coding: utf-8 -*-
"""Background worker for persisted and legacy alert rules."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from src.agent.events import (
    EventMonitor,
    PriceAlert,
    PriceChangeAlert,
    VolumeAlert,
    parse_event_alert_rules,
    validate_event_alert_rule,
)
from data_provider.base import normalize_stock_code
from data_provider.us_index_mapping import is_us_index_code
from src.analysis_context_pack_overview import (
    ANALYSIS_CONTEXT_PACK_OVERVIEW_KEY,
    extract_analysis_context_pack_overview,
)
from src.core.trading_calendar import (
    build_market_phase_context,
    get_market_for_stock,
    infer_market_phase,
    MarketPhase,
)
from src.market_phase_summary import (
    format_public_phase_pack_excerpt,
    render_market_phase_summary,
)
from src.services.alert_service import AlertService
from src.services.decision_signal_service import DecisionSignalService
from src.services.decision_signal_outcome_service import DecisionSignalOutcomeService
from src.services.decision_signal_summary import (
    format_decision_signal_excerpt,
    summarize_decision_signal,
)
from src.services.history_service import HistoryService
from src.services.market_light_service import normalize_market_alert_region

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.notification import ChannelAttemptResult, NotificationDispatchResult

ALERT_WORKER_FINGERPRINT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_DB_ALERT_COOLDOWN_SECONDS = 24 * 60 * 60
# 分钟级实时放量(volume_spike_rt)盘中持续监控：去重窗口与刷新周期对齐(~5min)，
# 不再复用 24h 默认，避免盘中触发一次后被一整天冷却压制。
REALTIME_ALERT_DEFAULT_COOLDOWN_SECONDS = 5 * 60
ALERT_WORKER_RULE_LIMIT = 1000
WRITABLE_TRIGGER_STATUSES = frozenset({"triggered", "skipped", "degraded", "failed"})

# Lazy DataFetcherManager for stock-name enrichment in notification titles.
_alert_worker_data_manager: Optional[Any] = None
_alert_worker_name_cache: Dict[str, Optional[str]] = {}


@dataclass
class RuntimeAlertRule:
    key: str
    rule: Any
    source: str
    severity: Optional[str] = None
    cooldown_policy: Optional[Dict[str, Any]] = None
    effective_target: Optional[str] = None
    display_target: Optional[str] = None


@dataclass
class DBCooldownDecision:
    suppressed: bool = False
    fallback_key: Optional[str] = None
    fallback_ttl_seconds: Optional[int] = None


@dataclass
class TriggerWriteResult:
    trigger_id: Optional[int] = None
    created: bool = False


class AlertWorker:
    """Evaluate alert-center rules for schedule-mode background polling."""

    def __init__(
        self,
        *,
        config_provider: Optional[Callable[[], Any]] = None,
        service: Optional[AlertService] = None,
        decision_signal_service: Optional[DecisionSignalService] = None,
        notifier: Optional[Any] = None,
        now_provider: Optional[Callable[[], float]] = None,
        fingerprint_ttl_seconds: int = ALERT_WORKER_FINGERPRINT_TTL_SECONDS,
    ) -> None:
        self.config_provider = config_provider or self._default_config_provider
        self.service = service or AlertService()
        self.decision_signal_service = decision_signal_service or DecisionSignalService()
        self.notifier = notifier
        self.now_provider = now_provider or time.time
        self.fingerprint_ttl_seconds = max(1, int(fingerprint_ttl_seconds))
        self._trigger_fingerprints: Dict[str, float] = {}
        self._trigger_fingerprint_ttls: Dict[str, int] = {}
        self._analysis_visibility_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    @staticmethod
    def _default_config_provider():
        from src.config import get_config

        return get_config()

    def run_once(self) -> Dict[str, int]:
        """Run one alert worker cycle.

        This method is intentionally exception-contained so scheduler background
        threads keep running even when one config or rule is bad.
        """
        stats = {
            "loaded": 0,
            "evaluated": 0,
            "recorded": 0,
            "triggered": 0,
            "notified": 0,
            "skipped": 0,
            "degraded": 0,
            "failed": 0,
            "notification_attempts": 0,
            "cooldown_suppressed": 0,
        }

        try:
            config = self.config_provider()
        except Exception as exc:
            logger.warning("[AlertWorker] Failed to load runtime config: %s", exc)
            return stats

        if not getattr(config, "agent_event_monitor_enabled", False):
            logger.debug("[AlertWorker] Event monitor disabled; skipping")
            return stats

        self._prune_fingerprints()
        runtime_rules = self._load_runtime_rules(config)
        stats["loaded"] = len(runtime_rules)
        if not runtime_rules:
            logger.info("[AlertWorker] No active alert rules loaded")
            return stats

        # Refresh realtime volume-ratio for any ``volume_spike_rt`` rules so
        # the slope window has fresh samples even when the web UI is idle.
        self._refresh_realtime_volume_cache(config, runtime_rules)

        monitor = EventMonitor()
        daily_cache: Dict[Any, Any] = {}
        self._analysis_visibility_cache = {}
        for runtime_rule in runtime_rules:
            stats["evaluated"] += 1
            try:
                result = asyncio.run(self.service._evaluate_rule(runtime_rule.rule, monitor, daily_cache=daily_cache))
            except Exception as exc:
                logger.warning("[AlertWorker] 规则评估异常 %s: %s", runtime_rule.rule.alert_type, exc)
                result = {
                    "rule_id": self.service._runtime_rule_id(runtime_rule.rule),
                    "record_status": "failed",
                    "triggered": False,
                    "observed_value": None,
                    "threshold": self.service._threshold_for_rule(runtime_rule.rule),
                    "data_source": self.service._data_source_for_rule(runtime_rule.rule),
                    "data_timestamp": None,
                    "reason": self.service._sanitize_text(str(exc) or "Alert evaluation failed"),
                    "message": self.service._sanitize_text(str(exc) or "Alert evaluation failed"),
                }

            record_status = result.get("record_status")
            if record_status == "triggered":
                logger.info("[AlertWorker] 触发 %s → %s: %s", runtime_rule.rule.alert_type, getattr(runtime_rule.rule, "stock_code", "?"), result.get("message", ""))
            elif record_status in ("failed", "skipped", "degraded"):
                logger.debug("[AlertWorker] %s %s: %s", record_status, getattr(runtime_rule.rule, "stock_code", "?"), result.get("message", ""))
            if record_status == "triggered":
                self._attach_decision_signal_summary_safely(runtime_rule, result)
            if record_status in WRITABLE_TRIGGER_STATUSES:
                trigger_write = self._record_trigger_safely(runtime_rule, result, record_status)
                trigger_id = trigger_write.trigger_id
                if trigger_write.created:
                    stats["recorded"] += 1
                if record_status in stats and record_status != "triggered":
                    stats[record_status] += 1
            else:
                trigger_id = None

            if record_status == "triggered":
                stats["triggered"] += 1
                if runtime_rule.source == "db":
                    cooldown_decision = self._check_db_cooldown(runtime_rule, trigger_id)
                    if cooldown_decision.suppressed:
                        stats["cooldown_suppressed"] += 1
                        stats["notification_attempts"] += 1
                        continue
                    dispatch = self._send_notification_safely(runtime_rule, result)
                    stats["notification_attempts"] += self._record_notification_attempts_safely(trigger_id, dispatch)
                    if self._dispatch_has_real_channel_success(dispatch):
                        self._upsert_db_cooldown_safely(runtime_rule, result)
                        if cooldown_decision.fallback_key:
                            self._mark_notified(
                                cooldown_decision.fallback_key,
                                ttl_seconds=cooldown_decision.fallback_ttl_seconds,
                            )
                        stats["notified"] += 1
                elif self._should_notify(runtime_rule.key):
                    dispatch = self._send_notification_safely(runtime_rule, result)
                    stats["notification_attempts"] += self._record_notification_attempts_safely(trigger_id, dispatch)
                    if bool(dispatch.success):
                        self._mark_notified(runtime_rule.key)
                        stats["notified"] += 1

        return stats

    def _load_runtime_rules(self, config: Any) -> List[RuntimeAlertRule]:
        runtime_rules: List[RuntimeAlertRule] = []
        seen_keys = set()

        for row in self.service.repo.list_enabled_rules(limit=ALERT_WORKER_RULE_LIMIT):
            try:
                cooldown_policy = self.service._load_json(row.cooldown_policy, default=None)
                for payload in self.service.build_runtime_payloads(row, config=config, include_overflow_payload=False):
                    if len(runtime_rules) >= ALERT_WORKER_RULE_LIMIT:
                        logger.warning(
                            "[AlertWorker] Runtime rule limit reached at %s; skipping remaining expanded rules",
                            ALERT_WORKER_RULE_LIMIT,
                        )
                        break
                    runtime_rules.append(
                        RuntimeAlertRule(
                            key=payload.key,
                            rule=payload.rule,
                            source="db",
                            severity=row.severity,
                            cooldown_policy=cooldown_policy,
                            effective_target=payload.effective_target,
                            display_target=payload.display_target,
                        )
                    )
                    seen_keys.add(payload.key)
                if len(runtime_rules) >= ALERT_WORKER_RULE_LIMIT:
                    break
            except Exception as exc:
                logger.warning("[AlertWorker] Skip invalid persisted alert rule %s: %s", getattr(row, "id", "?"), exc)

        for key, rule in self._load_legacy_rules(config):
            if key in seen_keys:
                logger.info("[AlertWorker] Skip duplicate legacy alert rule: %s", key)
                continue
            runtime_rules.append(RuntimeAlertRule(key=key, rule=rule, source="legacy_env"))
            seen_keys.add(key)

        return runtime_rules

    def _load_legacy_rules(self, config: Any) -> List[Tuple[str, Any]]:
        raw_rules = getattr(config, "agent_event_alert_rules_json", "")
        try:
            parsed_rules = parse_event_alert_rules(raw_rules)
        except Exception as exc:
            logger.warning("[AlertWorker] Failed to parse legacy alert rules: %s", exc)
            return []

        legacy_rules: List[Tuple[str, Any]] = []
        for index, entry in enumerate(parsed_rules, start=1):
            try:
                validate_event_alert_rule(entry)
                stock_code = str(entry.get("stock_code") or "").strip()
                alert_type = str(entry.get("alert_type") or "").strip().lower()
                parameters = self.service._normalize_parameters(alert_type, entry)
                key = self._semantic_key("single_symbol", stock_code, alert_type, parameters)
                metadata = {"source": "legacy_env", "legacy_rule_index": index}
                if alert_type == "price_cross":
                    rule = PriceAlert(
                        stock_code=stock_code,
                        direction=str(parameters["direction"]),
                        price=float(parameters["price"]),
                        metadata=metadata,
                    )
                elif alert_type == "price_change_percent":
                    rule = PriceChangeAlert(
                        stock_code=stock_code,
                        direction=str(parameters["direction"]),
                        change_pct=float(parameters["change_pct"]),
                        metadata=metadata,
                    )
                elif alert_type == "volume_spike":
                    rule = VolumeAlert(
                        stock_code=stock_code,
                        multiplier=float(parameters["multiplier"]),
                        metadata=metadata,
                    )
                else:
                    raise ValueError(f"unsupported alert_type: {alert_type}")
                legacy_rules.append((key, rule))
            except Exception as exc:
                logger.warning("[AlertWorker] Skip invalid legacy alert rule #%d: %s", index, exc)
        return legacy_rules

    @staticmethod
    def _semantic_key(target_scope: str, target: str, alert_type: str, parameters: Dict[str, Any]) -> str:
        canonical_params = json.dumps(parameters or {}, ensure_ascii=False, sort_keys=True)
        return f"{target_scope}:{target}:{alert_type}:{canonical_params}"

    def _record_trigger(self, runtime_rule: RuntimeAlertRule, result: Dict[str, Any], status: str) -> TriggerWriteResult:
        try:
            rule_id = int(result.get("rule_id") or 0) or None
        except (TypeError, ValueError):
            rule_id = None

        fields = {
            "rule_id": rule_id,
            "target": self._effective_target(runtime_rule),
            "observed_value": self._optional_float(result.get("observed_value")),
            "threshold": self._optional_float(result.get("threshold")),
            "reason": result.get("reason") or result.get("message"),
            "data_source": result.get("data_source"),
            "data_timestamp": result.get("data_timestamp"),
            "status": status,
            "diagnostics": self._diagnostics_for_status(status, result, runtime_rule),
        }
        if self._should_deduplicate_trigger(runtime_rule, fields):
            row, created = self.service.repo.create_trigger_if_absent(fields)
        else:
            row = self.service.repo.create_trigger(fields)
            created = True
        trigger_id = int(row.id) if row and row.id is not None else None
        return TriggerWriteResult(trigger_id=trigger_id, created=created)

    def _record_trigger_safely(
        self,
        runtime_rule: RuntimeAlertRule,
        result: Dict[str, Any],
        status: str,
    ) -> TriggerWriteResult:
        try:
            return self._record_trigger(runtime_rule, result, status)
        except Exception as exc:
            logger.warning(
                "[AlertWorker] Failed to record alert trigger for %s: %s",
                self._display_target(runtime_rule),
                self.service._sanitize_text(str(exc) or "trigger write failed"),
            )
            return TriggerWriteResult()

    @staticmethod
    def _should_deduplicate_trigger(runtime_rule: RuntimeAlertRule, fields: Dict[str, Any]) -> bool:
        return (
            runtime_rule.source == "db"
            and fields.get("status") == "triggered"
            and fields.get("rule_id") is not None
            and fields.get("data_timestamp") is not None
        )

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _diagnostics_for_status(
        self,
        status: str,
        result: Dict[str, Any],
        runtime_rule: RuntimeAlertRule,
    ) -> Optional[str]:
        if status == "triggered":
            payload = self._diagnostics_payload(result.get("diagnostics"))
            payload["analysis_visibility"] = self._build_analysis_visibility(runtime_rule, result)
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return result.get("message") or result.get("reason")

    @staticmethod
    def _diagnostics_payload(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"legacy_diagnostics": value}
            return dict(parsed) if isinstance(parsed, dict) else {"legacy_diagnostics": value}
        return {}

    def _attach_decision_signal_summary_safely(
        self,
        runtime_rule: RuntimeAlertRule,
        result: Dict[str, Any],
    ) -> None:
        try:
            summary = self._resolve_decision_signal_summary(runtime_rule, result)
            if not summary:
                return
            payload = self._diagnostics_payload(result.get("diagnostics"))
            payload["decision_signal_summary"] = summary
            result["diagnostics"] = payload
        except Exception as exc:
            logger.debug(
                "[AlertWorker] decision signal summary unavailable for %s: %s",
                self._display_target(runtime_rule),
                self.service._sanitize_text(str(exc) or "decision signal summary failed"),
            )

    def _resolve_decision_signal_summary(
        self,
        runtime_rule: RuntimeAlertRule,
        result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        identity = self._symbol_identity_for_decision_signal(runtime_rule)
        if identity is None:
            return None
        stock_code, market = identity
        latest = self.decision_signal_service.get_latest_active(
            stock_code=stock_code,
            market=market,
            limit=1,
        )
        items = latest.get("items") if isinstance(latest, dict) else None
        if items:
            return summarize_decision_signal(items[0])

        created = self.decision_signal_service.create_signal(
            self._alert_decision_signal_payload(
                runtime_rule,
                result,
                stock_code=stock_code,
                market=market,
            )
        )
        item = created.get("item") if isinstance(created, dict) else None
        return summarize_decision_signal(item)

    def _symbol_identity_for_decision_signal(self, runtime_rule: RuntimeAlertRule) -> Optional[Tuple[str, str]]:
        rule = getattr(runtime_rule, "rule", runtime_rule)
        metadata = getattr(rule, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
        target_scope = str(
            getattr(rule, "target_scope", None)
            or metadata.get("target_scope")
            or ""
        ).strip()
        if target_scope in {"market", "portfolio_account"}:
            return None
        target = str(
            metadata.get("effective_target")
            or runtime_rule.effective_target
            or getattr(rule, "stock_code", "")
            or ""
        ).strip()
        if not target or ":" in target:
            return None
        stock_code = normalize_stock_code(target)
        if is_us_index_code(stock_code):
            return None
        market = get_market_for_stock(stock_code)
        if market not in {"cn", "hk", "us"}:
            return None
        return stock_code, market

    def _alert_decision_signal_payload(
        self,
        runtime_rule: RuntimeAlertRule,
        result: Dict[str, Any],
        *,
        stock_code: str,
        market: str,
    ) -> Dict[str, Any]:
        rule = getattr(runtime_rule, "rule", runtime_rule)
        metadata = getattr(rule, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
        alert_type = self._public_alert_type(getattr(rule, "alert_type", None) or result.get("alert_type"))
        key_hash = hashlib.sha1(str(runtime_rule.key or "").encode("utf-8")).hexdigest()
        return {
            "stock_code": stock_code,
            "stock_name": getattr(rule, "stock_name", None),
            "market": market,
            "source_type": "alert",
            "source_agent": "alert_worker",
            "trace_id": f"alert-rule-{key_hash[:32]}",
            "trigger_source": "alert",
            "action": "alert",
            "reason": result.get("reason") or result.get("message") or getattr(rule, "description", None),
            "watch_conditions": self._alert_watch_conditions(runtime_rule, result, alert_type),
            "risk_summary": self._alert_risk_summary(runtime_rule, result),
            "metadata": {
                "rule_id": self.service._runtime_rule_id(rule),
                "alert_type": alert_type,
                "severity": runtime_rule.severity,
                "observed_value": result.get("observed_value"),
                "threshold": result.get("threshold"),
                "data_source": result.get("data_source"),
                "data_timestamp": self._iso_or_text(result.get("data_timestamp")),
                "grade": result.get("grade"),
                "stale_seconds": result.get("stale_seconds"),
                "data_channel": result.get("data_channel"),
                "rule_key_hash": key_hash,
            },
        }

    @staticmethod
    def _public_alert_type(value: Any) -> str:
        raw = getattr(value, "value", value)
        return str(raw or "").strip()[:64]

    def _alert_watch_conditions(
        self,
        runtime_rule: RuntimeAlertRule,
        result: Dict[str, Any],
        alert_type: str,
    ) -> str:
        threshold = result.get("threshold")
        observed = result.get("observed_value")
        target = self._display_target(runtime_rule)
        parts = [part for part in (target, alert_type) if part]
        if threshold not in (None, ""):
            parts.append(f"threshold={threshold}")
        if observed not in (None, ""):
            parts.append(f"observed={observed}")
        # #6 信号分级 + #8 行情时效/来源，便于事后复盘与权威性展示
        grade = result.get("grade")
        if grade:
            parts.append(f"信号强度={grade}")
        channel = result.get("data_channel")
        if channel:
            parts.append(f"来源={channel}")
        stale = result.get("stale_seconds")
        if stale is not None:
            parts.append(f"延迟={stale}s")
        return " | ".join(str(part) for part in parts)

    def _alert_risk_summary(self, runtime_rule: RuntimeAlertRule, result: Dict[str, Any]) -> str:
        severity = str(runtime_rule.severity or "warning")
        reason = result.get("reason") or result.get("message") or "Alert triggered"
        return f"{severity}: {reason}"

    @staticmethod
    def _iso_or_text(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    def _build_analysis_visibility(
        self,
        runtime_rule: RuntimeAlertRule,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        phase_summary = self._alert_market_phase_summary(runtime_rule)
        overview = self._evaluator_pack_overview(result)
        source = "evaluator_snapshot" if overview is not None else None
        if overview is None:
            overview = self._recent_history_pack_overview(runtime_rule)
            if overview is not None:
                source = "analysis_history_snapshot"
        return {
            "market_phase_summary": phase_summary,
            "analysis_context_pack_overview": overview,
            "source": source or "alert_trigger_market_context",
        }

    def _alert_market_phase_summary(self, runtime_rule: RuntimeAlertRule) -> Optional[Dict[str, Any]]:
        try:
            rule = getattr(runtime_rule, "rule", runtime_rule)
            target_scope = str(getattr(rule, "target_scope", "") or "")
            if target_scope == "market":
                market = normalize_market_alert_region(getattr(rule, "target", self._effective_target(runtime_rule)))
            elif target_scope in {"portfolio_account"}:
                market = None
            else:
                market = get_market_for_stock(normalize_stock_code(self._effective_target(runtime_rule)))
            context = build_market_phase_context(
                market=market,
                trigger_source="alert",
                analysis_phase="auto",
            )
            payload = context.to_dict() if hasattr(context, "to_dict") else context
            return render_market_phase_summary(payload)
        except Exception as exc:
            logger.debug("[AlertWorker] phase summary unavailable: %s", exc)
            return None

    @staticmethod
    def _evaluator_pack_overview(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        overview = result.get("analysis_context_pack_overview")
        if overview is None:
            diagnostics = result.get("diagnostics")
            if isinstance(diagnostics, str):
                try:
                    diagnostics = json.loads(diagnostics)
                except (TypeError, ValueError, json.JSONDecodeError):
                    diagnostics = None
            if isinstance(diagnostics, dict):
                overview = diagnostics.get("analysis_context_pack_overview")
        return extract_analysis_context_pack_overview({ANALYSIS_CONTEXT_PACK_OVERVIEW_KEY: overview})

    def _recent_history_pack_overview(self, runtime_rule: RuntimeAlertRule) -> Optional[Dict[str, Any]]:
        rule = getattr(runtime_rule, "rule", runtime_rule)
        target_scope = str(getattr(rule, "target_scope", "") or "")
        if target_scope in {"market", "portfolio_account"}:
            return None
        target = self._effective_target(runtime_rule)
        if not target or target == "?":
            return None
        cache_key = str(target).upper()
        if cache_key in self._analysis_visibility_cache:
            return self._analysis_visibility_cache[cache_key]
        overview: Optional[Dict[str, Any]] = None
        try:
            candidates = HistoryService._history_code_filter_candidates(target)
            records: List[Any] = []
            for candidate in candidates:
                records.extend(self.service.db.get_analysis_history(code=candidate, days=30, limit=1))
            records = sorted(records, key=lambda item: getattr(item, "created_at", None) or datetime.min, reverse=True)
            if records:
                overview = extract_analysis_context_pack_overview(getattr(records[0], "context_snapshot", None))
        except Exception as exc:
            logger.debug("[AlertWorker] recent history overview unavailable for %s: %s", target, exc)
            overview = None
        self._analysis_visibility_cache[cache_key] = overview
        return overview

    def _should_notify(self, rule_key: str, *, ttl_seconds: Optional[int] = None) -> bool:
        now = self.now_provider()
        last_seen = self._trigger_fingerprints.get(rule_key)
        ttl = self._fingerprint_ttl(rule_key, ttl_seconds=ttl_seconds)
        if last_seen is not None and now - last_seen < ttl:
            return False
        return True

    def _mark_notified(self, rule_key: str, *, ttl_seconds: Optional[int] = None) -> None:
        self._trigger_fingerprints[rule_key] = self.now_provider()
        if ttl_seconds is None:
            self._trigger_fingerprint_ttls.pop(rule_key, None)
        else:
            self._trigger_fingerprint_ttls[rule_key] = max(1, int(ttl_seconds))

    def _refresh_realtime_volume_cache(
        self, config: Any, runtime_rules: List["RuntimeAlertRule"]
    ) -> None:
        """Refresh realtime volume-ratio snapshots for any ``volume_spike_rt`` rules.

        Calling ``DataFetcherManager().get_realtime_quote`` triggers the
        ``_enrich_intraday_volume_ratio`` hook in ``data_provider/base.py``,
        which writes to the realtime volume cache automatically. We only call
        this for stocks that actually have a realtime rule, and respect the
        configured minimum refresh interval to avoid hammering the data source.
        """
        if not getattr(config, "enable_volume_spike_rt_cache", True):
            return
        from src.services.alert_indicators import REALTIME_ALERT_TYPES

        targets: Dict[str, str] = {}
        for runtime_rule in runtime_rules:
            rule = runtime_rule.rule
            if rule.alert_type not in REALTIME_ALERT_TYPES:
                continue
            if not getattr(rule, "is_active", True):
                continue
            stock = getattr(rule, "stock_code", None) or getattr(rule, "symbol", None)
            if stock:
                targets[stock] = runtime_rule.key

        if not targets:
            return

        refresh_interval = max(1, int(
            getattr(config, "volume_spike_rt_refresh_interval_seconds", 300)
        ))
        last_run = getattr(self, "_last_volume_spike_rt_refresh", 0.0)
        now = self.now_provider()
        if last_run and now - last_run < refresh_interval:
            return

        from data_provider.base import DataFetcherManager

        manager = DataFetcherManager()
        stock_codes = list(targets.keys())

        # #11 时段过滤：仅在与监控标的相同的市场“正在交易”时刷新实时量能缓存，
        #     避免收盘后/盘前 pytdx 服务器断开导致的 “无法连接任何服务器” 报错与假信号。
        sample_market = get_market_for_stock(stock_codes[0]) or "cn"
        phase = infer_market_phase(sample_market)
        if phase not in (MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION):
            logger.debug(
                "[AlertWorker] 非交易时段(%s)，跳过实时量能缓存刷新", phase.value
            )
            return

        # (1) 1 分钟 K 线独立喂入：以 60s 频率直接走 PytdxFetcher，与下面 300s 的
        #     spot-quote 量比刷新解耦，且**不依赖 get_realtime_quote 的多源 fallback**
        #     —— 这是修复「盘中主链路切换后 1 分钟预警消失」的核心。
        one_min_interval = max(1, int(
            getattr(config, "volume_spike_rt_1min_feed_interval_seconds", 60)
        ))
        last_1min = getattr(self, "_last_1min_kline_refresh", 0.0)
        if now - last_1min >= one_min_interval:
            try:
                manager.refresh_1min_kline_volume_ratios(stock_codes)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("[AlertWorker] 1min kline feed failed: %s", exc)
            self._last_1min_kline_refresh = now

        # (2) spot-quote 量比按 300s 刷新（维持原有行为）
        for stock_code in stock_codes:
            try:
                manager.get_realtime_quote(stock_code)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "[AlertWorker] realtime refresh failed for %s: %s",
                    stock_code, exc,
                )
        self._last_volume_spike_rt_refresh = now

    def _prune_fingerprints(self) -> None:
        now = self.now_provider()
        expired_keys = [
            key
            for key, last_seen in self._trigger_fingerprints.items()
            if now - last_seen >= self._fingerprint_ttl(key)
        ]
        for key in expired_keys:
            self._trigger_fingerprints.pop(key, None)
            self._trigger_fingerprint_ttls.pop(key, None)

    def _fingerprint_ttl(self, rule_key: str, *, ttl_seconds: Optional[int] = None) -> int:
        if ttl_seconds is not None:
            return max(1, int(ttl_seconds))
        return self._trigger_fingerprint_ttls.get(rule_key, self.fingerprint_ttl_seconds)

    @staticmethod
    def _db_cooldown_fallback_key(rule_key: str) -> str:
        return f"db_cooldown:{rule_key}"

    def _alert_historical_win_rate(self) -> Optional[str]:
        """#7 复用 DecisionSignalOutcomeService 统计系统历史同类预警信号胜率。

        返回形如 '历史预警信号胜率 68%（样本 142 次）'；样本不足或统计失败时返回 None。
        结果进程内缓存 30 分钟，避免每次触发都重算。
        """
        try:
            now = self.now_provider()
            cached = getattr(self, "_alert_win_rate_cache", None)
            if cached and now - cached[0] < 1800:
                return cached[1]
            outcome_service = DecisionSignalOutcomeService(db_manager=self.service.db)
            stats = outcome_service.get_stats()
            by_source = (stats.get("breakdowns") or {}).get("source_type") or {}
            alert_stats = by_source.get("alert") or {}
            sample = alert_stats.get("sample_size") or 0
            hit_rate = alert_stats.get("hit_rate")
            text: Optional[str] = None
            if sample and sample >= 20 and hit_rate is not None:
                text = f"历史预警信号胜率 {hit_rate:.0f}%（样本 {sample} 次）"
            self._alert_win_rate_cache = (now, text)
            return text
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[AlertWorker] 历史胜率统计失败: %s", exc)
            return None

    def _send_notification(self, runtime_rule: RuntimeAlertRule, result: Dict[str, Any]) -> "NotificationDispatchResult":
        from src.notification import NotificationBuilder, NotificationService
        from src.services.alert_indicators import REALTIME_ALERT_TYPES

        notification_service = self.notifier or NotificationService()
        direction_icon = {"up": "📈", "down": "📉", "flat": "➡️"}.get(
            result.get("direction"), ""
        )
        title_prefix = f"{direction_icon} Event Alert" if direction_icon else "Event Alert"
        # 分钟级实时放量(volume_spike_rt)区分来源：横盘选股自动注册 vs 用户自选股，
        # 标题加来源标签，避免两类通知文案无法区分。
        source_tag = ""
        rule = getattr(runtime_rule, "rule", None)
        if getattr(rule, "alert_type", None) in REALTIME_ALERT_TYPES:
            rule_source = getattr(rule, "source", None)
            source_tag = "横盘突破" if rule_source == "consolidation_breakout" else "自选股"
        title = f"{title_prefix} | {self._display_target(runtime_rule)}"
        if source_tag:
            title = f"{title_prefix} | {source_tag}｜{self._display_target(runtime_rule)}"
        content = result.get("reason") or result.get("message") or runtime_rule.rule.description or "Alert triggered"

        # #6 信号分级 + #8 行情时效/来源 已写入 summary（reason）；此处仅追加 #7 历史胜率
        win_rate = self._alert_historical_win_rate()
        if win_rate:
            content = f"{content}\n{win_rate}"

        diagnostics = self._diagnostics_payload(result.get("diagnostics"))
        visibility = diagnostics.get("analysis_visibility") if isinstance(diagnostics.get("analysis_visibility"), dict) else None
        if visibility is None:
            visibility = self._build_analysis_visibility(runtime_rule, result)
        excerpt = format_public_phase_pack_excerpt(
            visibility.get("market_phase_summary"),
            visibility.get("analysis_context_pack_overview"),
            source=visibility.get("source"),
        )
        if excerpt:
            content = f"{content}\n\n{excerpt}"
        signal_excerpt = format_decision_signal_excerpt(diagnostics.get("decision_signal_summary"))
        if signal_excerpt:
            content = f"{content}\n\n{signal_excerpt}"
        alert_text = NotificationBuilder.build_simple_alert(title=title, content=content, alert_type="warning")

        return notification_service.send_with_results(alert_text, route_type="alert")

    def _send_notification_safely(self, runtime_rule: RuntimeAlertRule, result: Dict[str, Any]) -> "NotificationDispatchResult":
        try:
            return self._send_notification(runtime_rule, result)
        except Exception as exc:
            from src.notification import ChannelAttemptResult, NotificationDispatchResult

            sanitized = self.service._sanitize_text(str(exc) or "notification failed")
            logger.warning(
                "[AlertWorker] Failed to send alert notification for %s: %s",
                self._display_target(runtime_rule),
                sanitized,
            )
            return NotificationDispatchResult(
                dispatched=False,
                success=False,
                status="exception",
                channel_results=[
                    ChannelAttemptResult(
                        channel="__dispatch__",
                        success=False,
                        error_code="exception",
                        retryable=True,
                        diagnostics=sanitized,
                    )
                ],
                message=sanitized,
            )

    def _record_notification_attempts_safely(
        self,
        trigger_id: Optional[int],
        dispatch: "NotificationDispatchResult",
    ) -> int:
        try:
            return self._record_notification_attempts(trigger_id, dispatch)
        except Exception as exc:
            logger.warning(
                "[AlertWorker] Failed to record alert notification attempt: %s",
                self.service._sanitize_text(str(exc) or "notification attempt write failed"),
            )
            return 0

    def _record_notification_attempts(self, trigger_id: Optional[int], dispatch: "NotificationDispatchResult") -> int:
        channel_results = list(dispatch.channel_results or [])
        if not channel_results:
            channel_results = [self._synthetic_attempt_for_dispatch(dispatch)]

        recorded = 0
        for attempt_index, item in enumerate(channel_results, start=1):
            fields = {
                "trigger_id": trigger_id,
                "channel": str(item.channel or "__dispatch__")[:32],
                "attempt": attempt_index,
                "success": bool(item.success),
                "error_code": item.error_code,
                "retryable": bool(item.retryable),
                "latency_ms": self._optional_int(item.latency_ms),
                "diagnostics": self.service._sanitize_text(item.diagnostics or dispatch.message),
            }
            self.service.repo.record_notification_attempt(fields)
            recorded += 1
        return recorded

    @staticmethod
    def _synthetic_attempt_for_dispatch(dispatch: "NotificationDispatchResult") -> "ChannelAttemptResult":
        from src.notification import ChannelAttemptResult

        status = str(dispatch.status or "unknown")
        channel_by_status = {
            "noise_suppressed": "__noise_suppressed__",
            "no_channel": "__no_channel__",
            "exception": "__dispatch__",
        }
        success = bool(dispatch.success)
        return ChannelAttemptResult(
            channel=channel_by_status.get(status, "__dispatch__"),
            success=success,
            error_code=None if success else status,
            retryable=status not in {"noise_suppressed", "no_channel"},
            diagnostics=dispatch.message,
        )

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _dispatch_has_real_channel_success(dispatch: "NotificationDispatchResult") -> bool:
        if not dispatch.dispatched:
            return False
        for item in dispatch.channel_results or []:
            channel = str(item.channel or "")
            if item.success and not channel.startswith("__"):
                return True
        return False

    def _check_db_cooldown(self, runtime_rule: RuntimeAlertRule, trigger_id: Optional[int]) -> DBCooldownDecision:
        """Return the DB cooldown decision for this trigger.

        Active persisted cooldowns record a ``__cooldown__`` synthetic
        notification attempt. If reading the cooldown state fails, the worker
        uses the process-local fingerprint as a temporary guard so DB outages
        do not turn persisted rules into one-notification-per-cycle spam.
        """
        cooldown_seconds = self._cooldown_seconds(runtime_rule)
        if cooldown_seconds <= 0:
            return DBCooldownDecision()
        rule_id = self.service._runtime_rule_id(runtime_rule.rule)
        if rule_id <= 0:
            return DBCooldownDecision()

        now_dt = self._now_datetime()
        try:
            cooldown = self.service.repo.get_active_cooldown(
                rule_id=rule_id,
                target=self._effective_target(runtime_rule),
                severity=runtime_rule.severity,
                now=now_dt,
            )
        except Exception as exc:
            logger.warning(
                "[AlertWorker] Failed to read alert cooldown for %s: %s",
                self._display_target(runtime_rule),
                self.service._sanitize_text(str(exc) or "cooldown read failed"),
            )
            fallback_key = self._db_cooldown_fallback_key(runtime_rule.key)
            if self._should_notify(fallback_key, ttl_seconds=cooldown_seconds):
                return DBCooldownDecision(
                    suppressed=False,
                    fallback_key=fallback_key,
                    fallback_ttl_seconds=cooldown_seconds,
                )
            self._record_cooldown_read_failure_suppression(trigger_id, exc)
            return DBCooldownDecision(suppressed=True)

        if cooldown is None:
            return DBCooldownDecision()

        from src.notification import ChannelAttemptResult, NotificationDispatchResult

        self._record_notification_attempts_safely(
            trigger_id,
            NotificationDispatchResult(
                dispatched=False,
                success=False,
                status="cooldown_active",
                channel_results=[
                    ChannelAttemptResult(
                        channel="__cooldown__",
                        success=False,
                        error_code="cooldown_active",
                        retryable=False,
                        diagnostics=(
                            f"cooldown_until={cooldown.cooldown_until.isoformat()}"
                            if cooldown.cooldown_until else "cooldown active"
                        ),
                    )
                ],
                message="alert cooldown active",
            ),
        )
        return DBCooldownDecision(suppressed=True)

    def _record_cooldown_read_failure_suppression(self, trigger_id: Optional[int], exc: Exception) -> None:
        from src.notification import ChannelAttemptResult, NotificationDispatchResult

        sanitized = self.service._sanitize_text(str(exc) or "cooldown read failed")
        self._record_notification_attempts_safely(
            trigger_id,
            NotificationDispatchResult(
                dispatched=False,
                success=False,
                status="cooldown_read_failed",
                channel_results=[
                    ChannelAttemptResult(
                        channel="__cooldown_read_failed__",
                        success=False,
                        error_code="cooldown_read_failed",
                        retryable=False,
                        diagnostics=sanitized,
                    )
                ],
                message=sanitized,
            ),
        )

    def _upsert_db_cooldown_safely(self, runtime_rule: RuntimeAlertRule, result: Dict[str, Any]) -> None:
        cooldown_seconds = self._cooldown_seconds(runtime_rule)
        if cooldown_seconds <= 0:
            return
        rule_id = self.service._runtime_rule_id(runtime_rule.rule)
        if rule_id <= 0:
            return
        now_dt = self._now_datetime()
        try:
            self.service.repo.upsert_cooldown(
                rule_id=rule_id,
                rule_key=runtime_rule.key,
                target=self._effective_target(runtime_rule),
                severity=runtime_rule.severity,
                last_triggered_at=now_dt,
                cooldown_until=now_dt + timedelta(seconds=cooldown_seconds),
                reason=self.service._sanitize_text(result.get("reason") or result.get("message")),
            )
        except Exception as exc:
            logger.warning(
                "[AlertWorker] Failed to update alert cooldown for %s: %s",
                self._display_target(runtime_rule),
                self.service._sanitize_text(str(exc) or "cooldown write failed"),
            )

    @staticmethod
    def _effective_target(runtime_rule: RuntimeAlertRule) -> str:
        return str(runtime_rule.effective_target or getattr(runtime_rule.rule, "stock_code", "") or "?")

    @staticmethod
    def _display_target(runtime_rule: RuntimeAlertRule) -> str:
        code = str(
            runtime_rule.display_target
            or runtime_rule.effective_target
            or getattr(runtime_rule.rule, "stock_code", "")
            or "?"
        )
        name = AlertWorker._stock_name_for_code(code)
        if name and name != code and not name.startswith("股票"):
            return f"{name} {code}"
        return code

    @staticmethod
    def _stock_name_for_code(stock_code: str) -> Optional[str]:
        if not stock_code or stock_code == "?":
            return None
        cached = _alert_worker_name_cache.get(stock_code)
        if cached is not None or stock_code in _alert_worker_name_cache:
            return cached
        normalized = normalize_stock_code(stock_code)
        if not normalized:
            _alert_worker_name_cache[stock_code] = None
            return None
        try:
            manager = AlertWorker._data_manager()
            name = manager.get_stock_name(normalized)
        except Exception:
            name = None
        _alert_worker_name_cache[stock_code] = name
        return name

    @staticmethod
    def _data_manager() -> Any:
        global _alert_worker_data_manager
        if _alert_worker_data_manager is None:
            from data_provider.base import DataFetcherManager
            _alert_worker_data_manager = DataFetcherManager()
        return _alert_worker_data_manager

    @staticmethod
    def _cooldown_seconds(runtime_rule: RuntimeAlertRule) -> int:
        from src.services.alert_indicators import REALTIME_ALERT_TYPES

        policy = runtime_rule.cooldown_policy if isinstance(runtime_rule.cooldown_policy, dict) else None
        if not policy or "cooldown_seconds" not in policy:
            # 分钟级实时放量(volume_spike_rt)盘中持续监控：去重窗口与刷新周期对齐，
            # 不沿用 24h 默认，否则盘中触发一次即被冷却一整天。
            alert_type = getattr(getattr(runtime_rule, "rule", None), "alert_type", None)
            if alert_type in REALTIME_ALERT_TYPES:
                return REALTIME_ALERT_DEFAULT_COOLDOWN_SECONDS
            return DEFAULT_DB_ALERT_COOLDOWN_SECONDS
        try:
            return max(0, int(policy.get("cooldown_seconds") or 0))
        except (TypeError, ValueError):
            return 0

    def _now_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.now_provider())
