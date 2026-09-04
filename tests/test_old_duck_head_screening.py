# -*- coding: utf-8 -*-
"""老鸭头（Old Duck Head）选股策略回归测试。

覆盖真实风险路径（不是只验证局部实现）：
  1. 形态特征列计算：合成 K 线必须算出鸭颈/鸭头/鸭嘴/量芝麻，且无鸭颈时返回 None
  2. 因子评分：真形态高分，五类假形态显著低分（门禁 + 乘性衰减不被 clip 掩盖）
  3. 硬过滤端到端：真形态保留，假形态（无鸭颈/60线下行/破位/量芝麻不足/
     鸭头顶太近/高位诱多/鸭颈过久）全部淘汰
"""
from __future__ import annotations

import pandas as pd

from src.services.screening.config import Config
from src.services.screening.daily import _compute_old_duck_head_features
from src.services.screening.filter import apply_hard_filters
from src.services.screening.scorer import (
    _DEFAULT_SCORING_PROFILE,
    _compute_old_duck_head_quality_score,
)
from src.services.screening.strategy import load_all_strategies


def _build_duck_head_frame() -> pd.DataFrame:
    """构造标准老鸭头：横盘 -> 鸭颈放量拉升 -> 鸭头缩量回调 -> 鸭嘴放量再涨。"""
    close: list[float] = []
    volume: list[float] = []
    close += [10.0] * 70
    volume += [100.0] * 70
    for i in range(20):  # 鸭颈：拉升，放量
        close.append(10.0 + 0.25 * (i + 1))
        volume.append(300.0)
    for i in range(15):  # 鸭头：回调，缩量（量芝麻点）
        close.append(15.0 - 0.1 * (i + 1))
        volume.append(80.0)
    for i in range(15):  # 鸭嘴：再涨，放量
        close.append(13.5 + 0.15 * (i + 1))
        volume.append(350.0)
    c = pd.Series(close[:120], dtype="float64")
    v = pd.Series(volume[:120], dtype="float64")
    return pd.DataFrame(
        {"close": c, "high": c * 1.01, "low": c * 0.99, "open": c, "volume": v}
    )


def test_features_detects_duck_head() -> None:
    feat = _compute_old_duck_head_features(_build_duck_head_frame())
    # 鸭颈已形成
    assert feat["barslast_ma5_cross_ma60"] is not None
    assert feat["barslast_ma5_cross_ma60"] > 0
    # 鸭嘴：死叉后再金叉
    assert feat["death_then_golden_5_10"] is True
    # 量芝麻点：回调缩量(80) vs 放量段(350) -> 峰/谷 >= 2
    assert feat["duck_beak_volume_contraction"] >= 2.0
    # 60 日均线上行（排除下行诱多）
    assert feat["ma60_slope_20d_pct"] > 0
    # 全程未有效跌破 60 日线
    assert feat["days_below_ma60_max"] == 0


def test_features_returns_none_without_neck() -> None:
    """长期横盘、从未上穿 MA60 的个股不应产出鸭颈特征（因子侧据此判 0 分）。"""
    n = 120
    c = pd.Series([10.0] * n, dtype="float64")
    df = pd.DataFrame(
        {
            "close": c,
            "high": c * 1.01,
            "low": c * 0.99,
            "open": c,
            "volume": pd.Series([100.0] * n, dtype="float64"),
        }
    )
    feat = _compute_old_duck_head_features(df)
    assert feat["barslast_ma5_cross_ma60"] is None


def _score_row(**overrides: object) -> float:
    row = {
        "barslast_ma5_cross_ma60": 30,
        "duck_nose_gap_pct": 0.3,
        "death_then_golden_5_10": True,
        "coiled_spring_ramp_ratio": 2.0,
        "duck_beak_volume_contraction": 4.4,
        "ma60_slope_20d_pct": 12.8,
        "duck_head_ma60_gap_pct": 21.4,
        "days_below_ma60_max": 0,
        "change_20d": 5.0,
    }
    row.update(overrides)  # type: ignore[arg-type]
    df = pd.DataFrame([row])
    score = _compute_old_duck_head_quality_score(df, dict(_DEFAULT_SCORING_PROFILE))
    return float(score.iloc[0])


def test_score_rewards_true_pattern() -> None:
    assert _score_row() >= 90.0


def test_score_penalizes_fake_patterns() -> None:
    true_score = _score_row()
    # 无鸭颈：形态不成立，门禁归零
    assert _score_row(barslast_ma5_cross_ma60=None) == 0.0
    # 60 日均线下行 = 反弹诱多
    assert _score_row(ma60_slope_20d_pct=-8.0) <= true_score * 0.6
    # 有效跌破 60 日线
    assert _score_row(days_below_ma60_max=4) <= true_score * 0.6
    # 鸭嘴无量突破 + 量芝麻不足 + 追高
    assert (
        _score_row(
            coiled_spring_ramp_ratio=0.8,
            duck_beak_volume_contraction=1.5,
            change_20d=25.0,
        )
        <= true_score * 0.4
    )


def test_hard_filters_keep_true_pattern_only() -> None:
    strategies = load_all_strategies(Config.from_env().strategies_dir)
    strategy = strategies["old_duck_head"]
    base = {
        "amount": 2e8,
        "price": 20,
        "turnover_rate": 3.0,
        "change_pct": 2.0,
        "change_60d": 25.0,
        "ma_bullish": True,
        "price_above_ma20": True,
        "signal_score": 70,
        "macd_status": "bullish",
        "range_20d_pct": 20.0,
        "volatility_20d_pct": 25.0,
        "max_drawdown_20d_pct": -15.0,
        "atr_20_pct": 4.0,
        "volume_ratio_20d": 1.5,
        "barslast_ma5_cross_ma60": 30,
        "duck_beak_volume_contraction": 4.4,
        "ma60_slope_20d_pct": 8.0,
        "days_below_ma60_max": 0,
        "duck_head_ma60_gap_pct": 18.0,
    }
    cases = {
        "真老鸭头": {},
        "无鸭颈": {"barslast_ma5_cross_ma60": None},
        "60线下行": {"ma60_slope_20d_pct": -5.0},
        "破位": {"days_below_ma60_max": 5},
        "量芝麻不足": {"duck_beak_volume_contraction": 1.4},
        "鸭头顶太近": {"duck_head_ma60_gap_pct": 3.0},
        "高位诱多": {"change_60d": 120.0},
        "鸭颈过久": {"barslast_ma5_cross_ma60": 90},
    }
    df = pd.DataFrame(
        [{**base, "name": name, **overrides} for name, overrides in cases.items()]
    )
    kept = apply_hard_filters(df, strategy.screening.hard_filters)
    assert kept["name"].tolist() == ["真老鸭头"]
