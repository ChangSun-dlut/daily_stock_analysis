# -*- coding: utf-8 -*-
"""AlphaSift stock screening API routes."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.deps import get_config_dep
from api.v1.errors import api_error
from src.config import Config
from src.services.alphasift_backtest_service import run_backtest, run_yesterday_backtest
from src.services.screening_service import (
    AlphaSiftService,
    get_sector_moneyflow,
    get_sector_rotation,
    read_alphasift_screen_cache,
    read_alphasift_screen_cache_on,
    read_alphasift_screen_cache_previous,
    write_alphasift_screen_cache,
)
from src.services.task_queue import TaskStatus as QueueTaskStatus
from src.services.task_queue import get_task_queue
from src.notification import NotificationService

router = APIRouter()


class AlphaSiftScreenRequest(BaseModel):
    market: str = Field("cn", min_length=1, max_length=16)
    strategy: str = Field("dual_low", min_length=1, max_length=64)
    max_results: int = Field(20, ge=1, le=100)
    daily_enrich_max_candidates: Optional[int] = Field(None, ge=1, le=1000)
    explain_filters: bool = Field(False)
    push: bool = Field(
        False,
        description="选股完成后是否通过默认通知渠道（如微信 OpenClaw）主动推送结果。"
        "Web 前端触发时为 False（用户自行查看）；微信 bot 触发时为 True。",
    )


class AlphaSiftStrategyResponse(BaseModel):
    id: str
    name: str = ""
    title: str = ""
    description: str = ""
    category: str = ""
    tag: str = ""
    tags: List[str] = Field(default_factory=list)
    market_scope: List[str] = Field(default_factory=list)
    market: str = ""


class AlphaSiftScreenAccepted(BaseModel):
    task_id: str
    trace_id: str
    status: str = "pending"
    message: str
    strategy: str
    market: str
    max_results: int


class AlphaSiftScreenTaskStatus(BaseModel):
    task_id: str
    trace_id: Optional[str] = None
    status: str
    progress: int = 0
    message: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


def _service(config: Config) -> AlphaSiftService:
    return AlphaSiftService(config=config)


def _format_screen_message(result: Dict[str, Any], strategy: str, market: str) -> str:
    """把选股结果格式化为适合微信推送的纯文本/轻量 markdown。"""
    from datetime import datetime, timezone

    date_str = (result.get("date") or datetime.now(timezone.utc).astimezone().date().isoformat())
    candidates: List[Dict[str, Any]] = result.get("candidates") or []
    header = f"📊 **{strategy} 选股 · {date_str}**\n"
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


def _push_screen_result(result: Dict[str, Any], strategy: str, market: str) -> None:
    """选股完成后通过默认通知渠道主动推送（不依赖来源消息）。"""
    from src.notification import NotificationService

    message = _format_screen_message(result, strategy, market)
    logger.info("选股完成，推送结果:\n%s", message)
    NotificationService(source_message=None).send(message, route_type="report")


def _screening_task_not_found(task_id: str) -> HTTPException:
    return api_error(
        404,
        "screening_screen_task_not_found",
        f"选股任务 {task_id} 不存在或已过期",
    )


@router.get("/status")
def alphasift_status(config: Config = Depends(get_config_dep)) -> Dict[str, Any]:
    return _service(config).status()


@router.get("/strategies")
def alphasift_strategies(
    request: Request,
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    return _service(config).strategies()


@router.get("/hotspots")
def alphasift_hotspots(
    provider: str = Query("", max_length=32),
    top: int = Query(12, ge=1, le=50),
    refresh: bool = Query(False),
    include_details: bool = Query(False),
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    refresh_value = refresh if isinstance(refresh, bool) else bool(getattr(refresh, "default", False))
    include_details_value = (
        include_details
        if isinstance(include_details, bool)
        else bool(getattr(include_details, "default", False))
    )
    return _service(config).hotspots(
        provider=provider,
        top=top,
        refresh=refresh_value,
        include_details=include_details_value,
    )


@router.get("/hotspots/{topic:path}")
def alphasift_hotspot_detail(
    topic: str,
    provider: str = Query("", max_length=32),
    refresh: bool = Query(False),
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    refresh_value = refresh if isinstance(refresh, bool) else bool(getattr(refresh, "default", False))
    return _service(config).hotspot_detail(topic=topic, provider=provider, refresh=refresh_value)


@router.get("/sector-moneyflow")
def alphasift_sector_moneyflow(
    top_n: int = Query(100, ge=1, le=500),
    refresh: bool = Query(False),
    _config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    """返回板块资金流向分析（主力净流入、净流出、中户/散户流向等）。"""
    refresh_value = refresh if isinstance(refresh, bool) else bool(getattr(refresh, "default", False))
    return get_sector_moneyflow(top_n=top_n, force_refresh=refresh_value)


@router.get("/sector-rotation")
def alphasift_sector_rotation(
    days: int = Query(10, ge=5, le=30),
    refresh: bool = Query(False),
    _config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    """板块轮动分析：最近一周板块上涨天数、连涨、退潮判断与明日布局预测。"""
    refresh_value = refresh if isinstance(refresh, bool) else bool(getattr(refresh, "default", False))
    return get_sector_rotation(days=days, force_refresh=refresh_value)


@router.post("/install")
def alphasift_install(
    request: Request,
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    return _service(config).install(request=request)


@router.post("/screen/tasks", status_code=202, response_model=AlphaSiftScreenAccepted)
def alphasift_start_screen_task(
    request: AlphaSiftScreenRequest,
    http_request: Request,
    config: Config = Depends(get_config_dep),
) -> AlphaSiftScreenAccepted:
    task_id = uuid.uuid4().hex
    task_queue = get_task_queue()

    def run_screen() -> Dict[str, Any]:
        task_queue.update_task_progress(
            task_id,
            20,
            "正在执行 AlphaSift 选股，外部数据源较慢时会持续后台运行",
        )
        try:
            result = _service(config).screen(
                strategy=request.strategy,
                market=request.market,
                max_results=request.max_results,
                daily_enrich_max_candidates=request.daily_enrich_max_candidates,
                explain_filters=request.explain_filters,
            )
        except Exception as exc:  # noqa: BLE001 - surface failure to caller/notify
            logger.error("选股任务 %s 失败: %s", task_id, exc, exc_info=True)
            if request.push:
                try:
                    NotificationService(source_message=None).send(
                        f"⚠️ {request.strategy} 选股失败：{exc}",
                        route_type="report",
                    )
                except Exception:  # noqa: BLE001 - never mask the original error
                    pass
            raise

        write_alphasift_screen_cache(
            strategy=request.strategy,
            market=request.market,
            result=result,
        )
        task_queue.update_task_progress(
            task_id,
            90,
            f"选股已完成，正在整理 {result.get('candidate_count', 0)} 条候选",
        )
        if request.push:
            try:
                _push_screen_result(result, request.strategy, request.market)
            except Exception as exc:  # noqa: BLE001 - push failure must not fail the task
                logger.error("选股任务 %s 结果推送失败: %s", task_id, exc, exc_info=True)
        return result

    task = task_queue.submit_background_task(
        run_screen,
        stock_code="alphasift_screen",
        stock_name=f"{request.strategy} / {request.market}",
        report_type="alphasift_screen",
        message="AlphaSift 选股任务已提交",
        task_id=task_id,
        trace_id=task_id,
    )
    return AlphaSiftScreenAccepted(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        message=task.message or "AlphaSift 选股任务已提交",
        strategy=request.strategy,
        market=request.market,
        max_results=request.max_results,
    )


@router.get("/screen/tasks/{task_id}", response_model=AlphaSiftScreenTaskStatus)
def screening_screen_task_status(task_id: str) -> AlphaSiftScreenTaskStatus:
    task = get_task_queue().get_task(task_id)
    if task is None or task.report_type != "alphasift_screen":
        raise _screening_task_not_found(task_id)

    result = task.result if task.status == QueueTaskStatus.COMPLETED and isinstance(task.result, dict) else None
    return AlphaSiftScreenTaskStatus(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        progress=task.progress,
        message=task.message,
        error=task.error,
        result=result,
    )


@router.get("/screen/cache/{strategy}")
def alphasift_screen_cache(strategy: str) -> dict | None:
    """返回某策略当天的最后一次选股缓存结果，无缓存返回空。"""
    cached = read_alphasift_screen_cache(strategy)
    if cached is None:
        return None
    return cached


@router.get("/screen/history/{strategy}")
def alphasift_screen_history(
    strategy: str,
    date: Optional[str] = Query(None, max_length=16),
) -> Dict[str, Any]:
    """返回某策略历史某天(默认最新且早于今天)的选股缓存，用于"昨日"Tab 展示真实昨日选股。

    返回结构与字段：
      - available: 是否有历史记录
      - date / market / cached_at: 该次选股的日期、市场、写入时间
      - candidates: [{code, name, price}] 列表（price 为选股时信号价，回测时会重算，可忽略）
      - candidate_count: 候选数量
    """
    cached = (
        read_alphasift_screen_cache_on(strategy, date)
        if date
        else read_alphasift_screen_cache_previous(strategy)
    )
    if cached is None:
        return {"available": False}
    raw_candidates = cached.get("candidates") or []
    candidates = [
        {
            "code": c.get("code") if isinstance(c, dict) else c,
            "name": c.get("name", "") if isinstance(c, dict) else "",
            "price": c.get("price") if isinstance(c, dict) else None,
        }
        for c in raw_candidates
        if isinstance(c, dict) and c.get("code")
    ]
    return {
        "available": True,
        "date": cached.get("date"),
        "market": cached.get("market"),
        "cached_at": cached.get("cached_at"),
        "candidates": candidates,
        "candidate_count": cached.get("candidate_count", len(candidates)),
    }


class BacktestCandidate(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    name: str = Field("", max_length=128)
    price: Optional[float] = Field(None)


class BacktestRequest(BaseModel):
    strategy: str = Field("consolidation_breakout", min_length=1, max_length=64)
    candidates: List[BacktestCandidate] = Field(..., min_length=1, max_length=50)


@router.post("/screen/backtest")
def alphasift_screen_backtest(
    request: BacktestRequest,
    _config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    """对当前选股结果执行一键回测，返回形态验证与盈亏分析。"""
    resp = run_backtest(
        candidates=[c.model_dump() for c in request.candidates],
        strategy=request.strategy,
    )
    # 把 dataclass 转为可 JSON 序列化的 dict
    return {
        "strategy": resp.strategy,
        "signal_date": resp.signal_date,
        "candidates": [
            {
                "code": c.code,
                "name": c.name,
                "signal_date": c.signal_date,
                "signal_price": c.signal_price,
                "pattern": {
                    "range_20d_pct": c.pattern.range_20d_pct if c.pattern else 0,
                    "volatility_20d_pct": c.pattern.volatility_20d_pct if c.pattern else 0,
                    "change_20d_pct": c.pattern.change_20d_pct if c.pattern else 0,
                    "consolidation_days": c.pattern.consolidation_days if c.pattern else 0,
                    "volume_ratio": c.pattern.volume_ratio if c.pattern else 0,
                    "signal_day_return_pct": c.pattern.signal_day_return_pct if c.pattern else 0,
                    "all_pass": c.pattern.all_pass if c.pattern else False,
                    "violations": c.pattern.violations if c.pattern else [],
                } if c.pattern else None,
                "holding_returns": [
                    {"days": h.days, "return_pct": h.return_pct} for h in c.holding_returns
                ],
                "max_return_5d_pct": c.max_return_5d_pct,
                "min_return_5d_pct": c.min_return_5d_pct,
                "ma_snapshots": [
                    {"ma": m.ma, "value": m.value, "price_above": m.price_above}
                    for m in c.ma_snapshots
                ],
                "window_simulations": [
                    {
                        "signal_date": w.signal_date,
                        "buy_price": w.buy_price,
                        "hold_days": w.hold_days,
                        "hold_return_pct": w.hold_return_pct,
                        "pattern_all_pass": w.pattern_all_pass,
                        "range_20d": w.range_20d,
                        "volatility_20d": w.volatility_20d,
                        "change_20d": w.change_20d,
                        "consolidation_days": w.consolidation_days,
                    }
                    for w in c.window_simulations
                ],
                "error": c.error,
                "fallback_source": c.fallback_source,
            }
            for c in resp.candidates
        ],
        "summary": resp.summary,
    }


@router.post("/screen/backtest/yesterday")
def alphasift_screen_backtest_yesterday(
    request: BacktestRequest,
    _config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    """昨日复盘——以昨日为信号日买入，评估今日实际回报并标注异常。

    异常判定规则：
      - 昨日形态不达标（横盘突破条件破坏）
      - 今日跌幅超过 -2%（-5% 以上为大幅下跌）
      - 昨日放量超过 3 倍
    """
    resp = run_yesterday_backtest(
        candidates=[c.model_dump() for c in request.candidates],
        strategy=request.strategy,
    )
    return {
        "candidates": [
            {
                "code": c.code,
                "name": c.name,
                "signal_date": c.signal_date,
                "today_date": c.today_date,
                "signal_price": c.signal_price,
                "today_open": c.today_open,
                "today_close": c.today_close,
                "today_high": c.today_high,
                "today_low": c.today_low,
                "today_return_pct": c.today_return_pct,
                "today_high_return_pct": c.today_high_return_pct,
                "today_low_return_pct": c.today_low_return_pct,
                "pattern": {
                    "range_20d_pct": c.pattern.range_20d_pct if c.pattern else 0,
                    "volatility_20d_pct": c.pattern.volatility_20d_pct if c.pattern else 0,
                    "change_20d_pct": c.pattern.change_20d_pct if c.pattern else 0,
                    "consolidation_days": c.pattern.consolidation_days if c.pattern else 0,
                    "volume_ratio": c.pattern.volume_ratio if c.pattern else 0,
                    "signal_day_return_pct": c.pattern.signal_day_return_pct if c.pattern else 0,
                    "all_pass": c.pattern.all_pass if c.pattern else False,
                    "violations": c.pattern.violations if c.pattern else [],
                } if c.pattern else None,
                "volume_ratio": c.volume_ratio,
                "has_anomaly": c.has_anomaly,
                "anomaly_reasons": c.anomaly_reasons,
                "error": c.error,
                "fallback_source": c.fallback_source,
                "trend": {
                    "trend_status": c.trend.trend_status,
                    "trend_strength": c.trend.trend_strength,
                    "ma_alignment": c.trend.ma_alignment,
                    "ma5": c.trend.ma5,
                    "ma10": c.trend.ma10,
                    "ma20": c.trend.ma20,
                    "ma60": c.trend.ma60,
                    "bias_ma5": c.trend.bias_ma5,
                    "volume_status": c.trend.volume_status,
                    "volume_ratio_5d": c.trend.volume_ratio_5d,
                    "macd_dif": c.trend.macd_dif,
                    "macd_dea": c.trend.macd_dea,
                    "macd_bar": c.trend.macd_bar,
                    "macd_status": c.trend.macd_status,
                    "macd_signal": c.trend.macd_signal,
                    "rsi_6": c.trend.rsi_6,
                    "rsi_12": c.trend.rsi_12,
                    "rsi_24": c.trend.rsi_24,
                    "rsi_status": c.trend.rsi_status,
                    "buy_signal": c.trend.buy_signal,
                    "signal_score": c.trend.signal_score,
                } if c.trend else None,
            }
            for c in resp.candidates
        ],
        "total": resp.total,
        "anomaly_count": resp.anomaly_count,
        "avg_return_pct": resp.avg_return_pct,
        "signal_date": resp.signal_date,
        "today_date": resp.today_date,
        "errors": resp.errors,
    }


@router.post("/screen")
def alphasift_screen(
    request: AlphaSiftScreenRequest,
    http_request: Request,
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    return _service(config).screen(
        strategy=request.strategy,
        market=request.market,
        max_results=request.max_results,
        daily_enrich_max_candidates=request.daily_enrich_max_candidates,
        explain_filters=request.explain_filters,
    )
