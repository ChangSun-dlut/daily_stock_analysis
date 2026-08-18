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
    read_alphasift_screen_cache,
    write_alphasift_screen_cache,
)
from src.services.task_queue import TaskStatus as QueueTaskStatus
from src.services.task_queue import get_task_queue

router = APIRouter()


class AlphaSiftScreenRequest(BaseModel):
    market: str = Field("cn", min_length=1, max_length=16)
    strategy: str = Field("dual_low", min_length=1, max_length=64)
    max_results: int = Field(20, ge=1, le=100)
    daily_enrich_max_candidates: Optional[int] = Field(None, ge=1, le=1000)
    explain_filters: bool = Field(False)


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


def _screening_task_not_found(task_id: str) -> HTTPException:
    return api_error(
        404,
        "alphasift_screen_task_not_found",
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
    _config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    """返回板块资金流向分析（主力净流入、净流出、中户/散户流向等）。"""
    return get_sector_moneyflow(top_n=top_n)


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
        result = _service(config).screen(
            strategy=request.strategy,
            market=request.market,
            max_results=request.max_results,
            daily_enrich_max_candidates=request.daily_enrich_max_candidates,
            explain_filters=request.explain_filters,
        )
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
def alphasift_screen_task_status(task_id: str) -> AlphaSiftScreenTaskStatus:
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
