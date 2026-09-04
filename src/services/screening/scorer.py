# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""screen_score calculation."""

import pandas as pd

from src.services.screening.models import ScreeningConfig

_FACTOR_COLUMNS = {
    "value": "factor_value_score",
    "liquidity": "factor_liquidity_score",
    "momentum": "factor_momentum_score",
    "reversal": "factor_reversal_score",
    "activity": "factor_activity_score",
    "stability": "factor_stability_score",
    "size": "factor_size_score",
    "theme_heat": "factor_theme_heat_score",
    "topic_alignment": "factor_topic_alignment_score",
    "consolidation_quality": "factor_consolidation_quality_score",
    "old_duck_head_quality": "factor_old_duck_head_quality_score",
    "bottom_accumulation_quality": "factor_bottom_accumulation_quality_score",
    "capital_heat_quality": "factor_capital_heat_quality_score",
    "sector_limitup_ladder": "factor_sector_limitup_ladder_score",
}
_DEFAULT_SCORING_PROFILE = {
    # === 老鸭头（old_duck_head_quality）===
    # 阈值来自多源实战共识：鸭颈放量上穿 MA60、鸭头缩量回调（量芝麻点 峰/谷 >= 2）、
    # 鸭嘴放量突破；并排除 60 线下行诱多、回调放量出货、连续 3 日破 60 线、高位诱多。
    "old_duck_head_quality_base": 50.0,
    "old_duck_head_barslast_ideal_min": 15.0,
    "old_duck_head_barslast_ideal_max": 45.0,
    "old_duck_head_barslast_bonus": 6.0,
    "old_duck_head_barslast_penalty_slope": 0.6,
    "old_duck_head_nose_gap_ideal": 0.5,
    "old_duck_head_nose_gap_bonus": 8.0,
    "old_duck_head_nose_gap_penalty_slope": 4.0,
    "old_duck_head_death_golden_bonus": 8.0,
    "old_duck_head_ramp_ratio_ideal_min": 1.15,
    "old_duck_head_ramp_ratio_ideal_max": 3.5,
    "old_duck_head_ramp_ratio_bonus": 10.0,
    "old_duck_head_ramp_ratio_penalty_slope": 25.0,
    "old_duck_head_ramp_ratio_weak": 1.0,
    "old_duck_head_ramp_weak_decay": 0.5,
    "old_duck_head_volume_contraction_ideal": 3.0,
    "old_duck_head_volume_contraction_bonus": 8.0,
    "old_duck_head_volume_contraction_penalty_slope": 8.0,
    "old_duck_head_ma60_slope_min": 0.0,
    "old_duck_head_ma60_slope_bonus": 6.0,
    "old_duck_head_ma60_slope_decay": 0.5,
    "old_duck_head_head_gap_ideal_min": 8.0,
    "old_duck_head_head_gap_bonus": 6.0,
    "old_duck_head_head_gap_penalty_slope": 1.5,
    "old_duck_head_days_below_ma60_max_allowed": 2.0,
    "old_duck_head_days_below_ma60_bonus": 4.0,
    "old_duck_head_days_below_ma60_decay": 0.4,
    "old_duck_head_chase_start_pct": 9.3,
    "old_duck_head_chase_penalty_slope": 1.5,
    "momentum_base": 60.0,
    "momentum_intraday_slope": 5.0,
    "momentum_chase_start_pct": 5.0,
    "momentum_chase_penalty_slope": 10.0,
    "momentum_downside_start_pct": -2.0,
    "momentum_downside_penalty_slope": 3.0,
    "momentum_60d_base": 55.0,
    "momentum_60d_slope": 0.9,
    "momentum_60d_overheat_pct": 45.0,
    "momentum_60d_overheat_penalty_slope": 0.8,
    "momentum_60d_breakdown_pct": -20.0,
    "momentum_60d_breakdown_penalty_slope": 0.7,
    "macd_bullish_bonus": 6.0,
    "macd_bearish_penalty": 8.0,
    "reversal_ideal_change_pct": -3.0,
    "reversal_distance_penalty_slope": 13.0,
    "reversal_collapse_start_pct": -8.0,
    "reversal_collapse_penalty_slope": 10.0,
    "reversal_chase_start_pct": 1.0,
    "reversal_chase_penalty_slope": 8.0,
    "rsi_oversold_bonus": 10.0,
    "rsi_overbought_penalty": 14.0,
    "activity_ideal_volume_ratio": 2.0,
    "activity_volume_ratio_distance_slope": 15.0,
    "activity_high_volume_ratio": 5.0,
    "activity_high_volume_ratio_penalty_slope": 8.0,
    "activity_ideal_turnover_rate": 4.0,
    "activity_turnover_distance_slope": 8.0,
    "activity_high_turnover_rate": 12.0,
    "activity_high_turnover_penalty_slope": 5.0,
    "stability_base": 78.0,
    "stability_change_abs_penalty_slope": 3.0,
    "stability_hot_change_pct": 7.0,
    "stability_hot_change_penalty_slope": 5.0,
    "stability_high_turnover_rate": 10.0,
    "stability_high_turnover_penalty_slope": 2.0,
    "stability_high_volume_ratio": 5.0,
    "stability_high_volume_ratio_penalty_slope": 4.0,
    "stability_invalid_pe_penalty": 18.0,
    "stability_high_volatility_pct": 45.0,
    "stability_high_volatility_penalty_slope": 0.45,
    "stability_max_drawdown_floor_pct": -12.0,
    "stability_drawdown_penalty_slope": 1.2,
    "stability_high_atr_pct": 6.0,
    "stability_high_atr_penalty_slope": 2.0,
    "stability_low_daily_quality_score": 80.0,
    "stability_low_daily_quality_penalty_slope": 0.35,
    "stability_bad_daily_quality_flag_penalty": 8.0,
    "theme_heat_unknown_score": 50.0,
    "theme_heat_change_slope": 6.0,
    "theme_heat_rank_bonus": 10.0,
    "theme_heat_trend_min_observations": 2.0,
    "theme_heat_trend_slope": 0.8,
    "theme_heat_trend_bonus_cap": 10.0,
    "theme_heat_cooling_penalty_slope": 0.8,
    "theme_heat_cooling_penalty_cap": 12.0,
    "theme_heat_persistence_min_score": 60.0,
    "theme_heat_persistence_slope": 0.08,
    "theme_heat_persistence_bonus_cap": 6.0,
    "theme_heat_cooling_score_penalty_slope": 0.6,
    "theme_heat_cooling_score_penalty_cap": 10.0,
    "theme_heat_overheat_score": 88.0,
    "theme_heat_overheat_penalty_slope": 0.5,
    "topic_alignment_unknown_score": 50.0,
    "topic_alignment_match_bonus": 25.0,
    "topic_alignment_heat_weight": 0.25,
    "topic_alignment_unmatched_penalty": 12.0,
    "consolidation_quality_base": 50.0,
    "consolidation_quality_breakout_bonus_slope": 0.80,
    "consolidation_quality_breakout_threshold_pct": 30.0,
    "consolidation_quality_days_ideal": 10.0,
    "consolidation_quality_days_slope": 4.0,
    "consolidation_quality_range_ideal_max": 12.0,
    "consolidation_quality_range_ideal_min": 2.0,
    "consolidation_quality_range_penalty_slope": 2.5,
    "consolidation_quality_volatility_ideal": 20.0,
    "consolidation_quality_volatility_penalty_slope": 1.0,
    "consolidation_quality_volume_ratio_ideal": 0.70,
    "consolidation_quality_volume_ratio_penalty_slope": 15.0,
    "consolidation_quality_overhead_threshold_pct": -15.0,
    "consolidation_quality_overhead_penalty_slope": 0.5,
    "consolidation_quality_near_high_threshold_pct": -5.0,
    "consolidation_quality_near_high_penalty": 25.0,
    "consolidation_quality_momentum_20d_threshold_pct": 5.0,
    "consolidation_quality_momentum_20d_penalty_slope": 4.0,
    "consolidation_quality_momentum_10d_threshold_pct": 3.0,
    "consolidation_quality_momentum_10d_penalty_slope": 4.0,
    "consolidation_quality_volume_expand_threshold": 1.0,
    "consolidation_quality_volume_expand_bonus": 12.0,
    "consolidation_quality_volume_spike_threshold": 1.5,
    "consolidation_quality_volume_spike_bonus_slope": 45.0,
    "consolidation_quality_consecutive_spike_2d_bonus": 8.0,
    "consolidation_quality_consecutive_spike_3d_bonus": 15.0,
    "consolidation_quality_coiled_spring_contraction_min": 15.0,
    "consolidation_quality_coiled_spring_ramp_min": 1.15,
    "consolidation_quality_coiled_spring_ramp_max": 3.5,
    "consolidation_quality_coiled_spring_bonus": 20.0,
    "consolidation_quality_surge_pullback_penalty_slope": 3.0,
    "consolidation_quality_surge_pullback_drawdown_threshold_pct": 8.0,
    "consolidation_quality_long_bonus_60d": 8.0,
    "consolidation_quality_long_bonus_120d": 15.0,
    "consolidation_quality_long_bonus_250d": 22.0,
    "consolidation_quality_ma_bullish_bonus": 5.0,
    "consolidation_quality_price_above_ma20_bonus": 3.0,
    # 破位惩罚（横盘蓄势相关）：大阴线 + 倍量柱 + 下穿多条均线
    "consolidation_quality_bear_candle_pct": -5.0,
    "consolidation_quality_bear_volume_ratio": 2.0,
    "consolidation_quality_bear_ma_breakdown_min": 2,
    "consolidation_quality_bear_penalty_each": 6.0,
    "consolidation_quality_bear_penalty_all_extra": 7.0,
    "consolidation_quality_bear_penalty_cap": 25.0,
    # 累积破位惩罚（横盘蓄势相关）：多日主力净流出 / 持续大幅回撤 / 60 日深跌破底
    "consolidation_quality_cum_mf_negative_5d_threshold": 0.0,
    "consolidation_quality_cum_prolonged_decline_20d_pct": -15.0,
    "consolidation_quality_cum_deep_break_60d_pct": -50.0,
    "consolidation_quality_cum_penalty_each": 4.0,
    "consolidation_quality_cum_penalty_cap": 20.0,
    # 强信号组合：均线已发散 + 5 日持续下行 + 当日仍跌（结构性破位）
    "consolidation_quality_cum_strong_ma_breakdown_min": 2,
    "consolidation_quality_cum_strong_prolonged_decline_5d_pct": -5.0,
    "consolidation_quality_cum_strong_penalty": 14.0,
    # --- bottom_accumulation_quality ---
    "bottom_accumulation_decline_sweet_min": 15.0,
    "bottom_accumulation_decline_sweet_max": 40.0,
    "bottom_accumulation_decline_too_deep_max": 60.0,
    "bottom_accumulation_rsi_oversold_threshold": 35.0,
    "bottom_accumulation_rsi_recovery_min": 5.0,
    "bottom_accumulation_volume_expand_min": 1.3,
    "bottom_accumulation_price_vs_60d_low_min": 3.0,
    "bottom_accumulation_ma5_turn_up_min": 1.0,
    "bottom_accumulation_signal_min_signals": 3,
    "bottom_accumulation_mf_inflow_5d_min": 500.0,
    "bottom_accumulation_mf_inflow_5d_max": 5000.0,
    "bottom_accumulation_mf_outflow_5d_min": -500.0,
    "bottom_accumulation_mf_outflow_5d_max": -5000.0,
    "bottom_accumulation_mf_strength_pct_min": 2.0,
    "bottom_accumulation_mf_strength_pct_max": 10.0,
    "bottom_accumulation_chase_20d_start_pct": 8.0,
    "bottom_accumulation_chase_20d_max_pct": 20.0,
    "bottom_accumulation_chase_10d_start_pct": 5.0,
    "bottom_accumulation_upper_shadow_threshold_pct": 50.0,
    "bottom_accumulation_upper_shadow_rise_min_pct": 3.0,
    # 破位惩罚（底部吸筹相关）：大阴线 + 倍量柱 + 下穿多条均线
    "bottom_accumulation_bear_candle_pct": -5.0,
    "bottom_accumulation_bear_volume_ratio": 2.0,
    "bottom_accumulation_bear_ma_breakdown_min": 2,
    "bottom_accumulation_bear_penalty_each": 6.0,
    "bottom_accumulation_bear_penalty_all_extra": 7.0,
    "bottom_accumulation_bear_penalty_cap": 25.0,
    # 累积破位惩罚（底部吸筹相关）：多日主力净流出 / 持续大幅回撤 / 60 日深跌破底
    "bottom_accumulation_cum_mf_negative_5d_threshold": 0.0,
    "bottom_accumulation_cum_prolonged_decline_20d_pct": -15.0,
    "bottom_accumulation_cum_deep_break_60d_pct": -50.0,
    "bottom_accumulation_cum_penalty_each": 4.0,
    "bottom_accumulation_cum_penalty_cap": 20.0,
    # 强信号组合：均线已发散 + 5 日持续下行 + 当日仍跌（结构性破位）
    "bottom_accumulation_cum_strong_ma_breakdown_min": 2,
    "bottom_accumulation_cum_strong_prolonged_decline_5d_pct": -5.0,
    "bottom_accumulation_cum_strong_penalty": 14.0,
    "consolidation_mf_inflow_5d_min": 300.0,
    "consolidation_mf_inflow_5d_max": 3000.0,
    "consolidation_mf_outflow_5d_min": -300.0,
    "consolidation_mf_outflow_5d_max": -3000.0,
    # capital_heat moneyflow scorecard
    "capital_heat_quality_base": 50.0,
    "capital_heat_mf_inflow_5d_min": 500.0,
    "capital_heat_mf_inflow_5d_max": 5000.0,
    "capital_heat_mf_outflow_5d_min": -500.0,
    "capital_heat_mf_outflow_5d_max": -5000.0,
    "capital_heat_mf_strength_pct_min": 3.0,
    "capital_heat_mf_strength_pct_max": 10.0,
    "capital_heat_capital_confirmed_bonus": 2.4,
    # sector_limitup_ladder（连板梯队 + 板块资金活跃）
    "sector_limitup_ladder_base": 50.0,
    "sector_limitup_ladder_2_limitup_bonus": 5.0,      # 同板块涨停 ≥2 家（形成梯队）
    "sector_limitup_ladder_4_limitup_bonus": 8.0,      # 同板块涨停 ≥4 家（梯队明显）
    "sector_limitup_ladder_6_limitup_bonus": 8.0,      # 同板块涨停 ≥6 家（强连板梯队）
    "sector_limitup_ladder_active_capital_bonus": 6.0, # 板块涨幅 >1%（资金活跃）叠加
    "sector_limitup_ladder_active_capital_pct": 1.0,
}


def compute_screen_scores(df: pd.DataFrame, config: ScreeningConfig) -> pd.DataFrame:
    """Compute screen_score for each candidate row.

    Adds a 'screen_score' column (0-100). Higher is better.
    """
    result = df.copy()
    factors = _compute_factor_scores(result, config)
    for name, series in factors.items():
        result[_FACTOR_COLUMNS[name]] = series.round(4)

    weights = _normalized_factor_weights(config)
    result["screen_score"] = 0.0
    for factor, weight in weights.items():
        if factor in factors:
            result["screen_score"] += factors[factor] * weight

    result["screen_score"] = result["screen_score"].clip(0, 100)

    return result


def factor_score_columns() -> dict[str, str]:
    """Return the stable factor-score column mapping used in Pick output."""
    return dict(_FACTOR_COLUMNS)


def _normalized_factor_weights(config: ScreeningConfig) -> dict[str, float]:
    """Use explicit factor weights, or derive a sane legacy default from tech_weight."""
    raw_weights = config.factor_weights or {
        "value": (1 - config.tech_weight) * 0.50,
        "liquidity": (1 - config.tech_weight) * 0.25,
        "stability": (1 - config.tech_weight) * 0.25,
        "momentum": config.tech_weight * 0.55,
        "activity": config.tech_weight * 0.45,
    }
    weights = {
        factor: max(float(weight), 0.0)
        for factor, weight in raw_weights.items()
        if factor in _FACTOR_COLUMNS
    }
    total = sum(weights.values())
    if total <= 0:
        return {"value": 0.4, "liquidity": 0.2, "momentum": 0.2, "activity": 0.2}
    return {factor: weight / total for factor, weight in weights.items()}


def _compute_factor_scores(df: pd.DataFrame, config: ScreeningConfig | None = None) -> dict[str, pd.Series]:
    config = config or ScreeningConfig()
    profile = _scoring_profile(config)
    return {
        "value": _compute_value_score(df),
        "liquidity": _compute_liquidity_score(df),
        "momentum": _compute_momentum_score(df, profile),
        "reversal": _compute_reversal_score(df, profile),
        "activity": _compute_activity_score(df, profile),
        "stability": _compute_stability_score(df, profile),
        "size": _compute_size_score(df),
        "theme_heat": _compute_theme_heat_score(df, profile),
        "topic_alignment": _compute_topic_alignment_score(df, profile),
        "consolidation_quality": _compute_consolidation_quality_score(df, profile),
        "old_duck_head_quality": _compute_old_duck_head_quality_score(df, profile),
        "bottom_accumulation_quality": _compute_bottom_accumulation_quality_score(df, profile),
        "capital_heat_quality": _compute_capital_heat_quality_score(df, profile),
        "sector_limitup_ladder": _compute_sector_limitup_ladder_score(df, profile),
    }


def _scoring_profile(config: ScreeningConfig) -> dict[str, float]:
    profile = dict(_DEFAULT_SCORING_PROFILE)
    for key, value in (config.scoring_profile or {}).items():
        if key in profile:
            profile[key] = float(value)
    return profile


def _compute_snapshot_score(df: pd.DataFrame) -> pd.Series:
    """Score based on snapshot fundamentals (0-100).

    Components:
    - PE ratio: lower is better (for value), normalized
    - PB ratio: lower is better, normalized
    - Turnover rate: moderate is best
    - Amount (liquidity): higher is better, log-scaled
    - Change pct: near zero or moderate positive preferred
    """
    factors = _compute_factor_scores(df)
    return (
        factors["value"] * 0.50
        + factors["liquidity"] * 0.25
        + factors["stability"] * 0.25
    ).clip(0, 100)


def _compute_tech_score(df: pd.DataFrame) -> pd.Series:
    """Score based on technical features (0-100).

    Uses available columns like volume_ratio, change_pct patterns.
    Full tech scoring (MA structure, MACD/RSI) needs daily data,
    which is not in the snapshot — scored conservatively here.
    """
    factors = _compute_factor_scores(df)
    return (factors["momentum"] * 0.55 + factors["activity"] * 0.45).clip(0, 100)


def _compute_value_score(df: pd.DataFrame) -> pd.Series:
    score = pd.Series(50.0, index=df.index)

    if "pe_ratio" in df.columns:
        pe = pd.to_numeric(df["pe_ratio"], errors="coerce")
        pe_score = _rank_score(pe.where((pe > 0) & (pe < 500)), lower_is_better=True, na_score=25)
        score = score * 0.35 + pe_score * 0.65

    if "pb_ratio" in df.columns:
        pb = pd.to_numeric(df["pb_ratio"], errors="coerce")
        pb_score = _rank_score(pb.where((pb > 0) & (pb < 50)), lower_is_better=True, na_score=25)
        score = score * 0.55 + pb_score * 0.45

    return score.clip(0, 100)


def _compute_liquidity_score(df: pd.DataFrame) -> pd.Series:
    if "amount" not in df.columns:
        return pd.Series(50.0, index=df.index)

    import numpy as np

    amount = pd.to_numeric(df["amount"], errors="coerce")
    log_amount = np.log10(amount.clip(lower=1))
    return _rank_score(log_amount.where(amount > 0), lower_is_better=False, na_score=20)


def _compute_momentum_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    score = pd.Series(50.0, index=df.index)

    if "change_pct" in df.columns:
        change = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0)
        # Prefer constructive positive moves, but penalize chase-risk near limit-up.
        intraday_score = profile["momentum_base"] + change * profile["momentum_intraday_slope"]
        intraday_score = intraday_score - (
            change - profile["momentum_chase_start_pct"]
        ).clip(lower=0) * profile["momentum_chase_penalty_slope"]
        intraday_score = intraday_score - (
            -change + profile["momentum_downside_start_pct"]
        ).clip(lower=0) * profile["momentum_downside_penalty_slope"]
        score = score * 0.35 + intraday_score.clip(5, 100) * 0.65

    if "change_60d" in df.columns:
        change_60d = pd.to_numeric(df["change_60d"], errors="coerce").fillna(0)
        trend_score = profile["momentum_60d_base"] + change_60d * profile["momentum_60d_slope"]
        trend_score = trend_score - (
            change_60d - profile["momentum_60d_overheat_pct"]
        ).clip(lower=0) * profile["momentum_60d_overheat_penalty_slope"]
        trend_score = trend_score - (
            -change_60d + profile["momentum_60d_breakdown_pct"]
        ).clip(lower=0) * profile["momentum_60d_breakdown_penalty_slope"]
        score = score * 0.60 + trend_score.clip(5, 100) * 0.40

    if "signal_score" in df.columns:
        signal = pd.to_numeric(df["signal_score"], errors="coerce").fillna(50)
        score = score * 0.70 + signal.clip(0, 100) * 0.30

    if "macd_status" in df.columns:
        macd = df["macd_status"].astype(str)
        score = score + macd.map({
            "bullish": profile["macd_bullish_bonus"],
            "bearish": -profile["macd_bearish_penalty"],
        }).fillna(0)

    return score.clip(5, 100)


def _compute_reversal_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    if "change_pct" not in df.columns:
        return pd.Series(50.0, index=df.index)

    change = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0)
    # Reversal setups prefer controlled weakness, not collapse.
    score = 100 - (
        change - profile["reversal_ideal_change_pct"]
    ).abs() * profile["reversal_distance_penalty_slope"]
    score = score - (
        -change + profile["reversal_collapse_start_pct"]
    ).clip(lower=0) * profile["reversal_collapse_penalty_slope"]
    score = score - (
        change - profile["reversal_chase_start_pct"]
    ).clip(lower=0) * profile["reversal_chase_penalty_slope"]

    if "rsi_status" in df.columns:
        rsi = df["rsi_status"].astype(str)
        score = score + rsi.map({
            "oversold": profile["rsi_oversold_bonus"],
            "overbought": -profile["rsi_overbought_penalty"],
        }).fillna(0)
    if "change_60d" in df.columns:
        change_60d = pd.to_numeric(df["change_60d"], errors="coerce").fillna(0)
        score = score - (change_60d - 35).clip(lower=0) * 0.5
        score = score - (-change_60d - 35).clip(lower=0) * 0.8
    return score.clip(5, 100)


def _compute_activity_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    score = pd.Series(50.0, index=df.index)

    if "volume_ratio" in df.columns:
        volume_ratio = pd.to_numeric(df["volume_ratio"], errors="coerce").fillna(1.0)
        vr_score = 100 - (
            volume_ratio - profile["activity_ideal_volume_ratio"]
        ).abs() * profile["activity_volume_ratio_distance_slope"]
        vr_score = vr_score - (
            volume_ratio - profile["activity_high_volume_ratio"]
        ).clip(lower=0) * profile["activity_high_volume_ratio_penalty_slope"]
        score = score * 0.45 + vr_score.clip(5, 100) * 0.55

    if "turnover_rate" in df.columns:
        turnover = pd.to_numeric(df["turnover_rate"], errors="coerce").fillna(0)
        turnover_score = 100 - (
            turnover - profile["activity_ideal_turnover_rate"]
        ).abs() * profile["activity_turnover_distance_slope"]
        turnover_score = turnover_score - (
            turnover - profile["activity_high_turnover_rate"]
        ).clip(lower=0) * profile["activity_high_turnover_penalty_slope"]
        turnover_score = turnover_score.where(turnover > 0, 40)
        score = score * 0.55 + turnover_score.clip(5, 100) * 0.45

    return score.clip(0, 100)


def _compute_stability_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    score = pd.Series(profile["stability_base"], index=df.index)

    if "change_pct" in df.columns:
        change = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0)
        score -= change.abs().clip(upper=10) * profile["stability_change_abs_penalty_slope"]
        score -= (
            change - profile["stability_hot_change_pct"]
        ).clip(lower=0) * profile["stability_hot_change_penalty_slope"]

    if "turnover_rate" in df.columns:
        turnover = pd.to_numeric(df["turnover_rate"], errors="coerce").fillna(0)
        score -= (
            turnover - profile["stability_high_turnover_rate"]
        ).clip(lower=0) * profile["stability_high_turnover_penalty_slope"]

    if "volume_ratio" in df.columns:
        volume_ratio = pd.to_numeric(df["volume_ratio"], errors="coerce").fillna(1)
        score -= (
            volume_ratio - profile["stability_high_volume_ratio"]
        ).clip(lower=0) * profile["stability_high_volume_ratio_penalty_slope"]

    if "pe_ratio" in df.columns:
        pe = pd.to_numeric(df["pe_ratio"], errors="coerce")
        score = score.where((pe.isna()) | (pe > 0), score - profile["stability_invalid_pe_penalty"])

    if "signal_score" in df.columns:
        signal = pd.to_numeric(df["signal_score"], errors="coerce").fillna(50)
        score = score + (signal - 50) * 0.12

    if "volatility_20d_pct" in df.columns:
        volatility = pd.to_numeric(df["volatility_20d_pct"], errors="coerce")
        score -= (
            volatility - profile["stability_high_volatility_pct"]
        ).clip(lower=0).fillna(0) * profile["stability_high_volatility_penalty_slope"]

    if "max_drawdown_20d_pct" in df.columns:
        drawdown = pd.to_numeric(df["max_drawdown_20d_pct"], errors="coerce")
        score -= (
            profile["stability_max_drawdown_floor_pct"] - drawdown
        ).clip(lower=0).fillna(0) * profile["stability_drawdown_penalty_slope"]

    if "atr_20_pct" in df.columns:
        atr = pd.to_numeric(df["atr_20_pct"], errors="coerce")
        score -= (
            atr - profile["stability_high_atr_pct"]
        ).clip(lower=0).fillna(0) * profile["stability_high_atr_penalty_slope"]

    if "daily_quality_score" in df.columns:
        quality = pd.to_numeric(df["daily_quality_score"], errors="coerce")
        score -= (
            profile["stability_low_daily_quality_score"] - quality
        ).clip(lower=0).fillna(0) * profile["stability_low_daily_quality_penalty_slope"]

    if "daily_quality_flags" in df.columns:
        flags = df["daily_quality_flags"].fillna("").astype(str)
        severe_flags = flags.str.contains("invalid_ohlc|non_positive_price|negative_volume|stale_cache")
        score -= severe_flags.astype(float) * profile["stability_bad_daily_quality_flag_penalty"]

    return score.clip(0, 100)


def _compute_size_score(df: pd.DataFrame) -> pd.Series:
    if "total_mv" not in df.columns:
        return pd.Series(50.0, index=df.index)

    import numpy as np

    mv = pd.to_numeric(df["total_mv"], errors="coerce")
    log_mv = np.log10(mv.clip(lower=1))
    return _rank_score(log_mv.where(mv > 0), lower_is_better=False, na_score=35)


def _compute_theme_heat_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    base = pd.Series(profile["theme_heat_unknown_score"], index=df.index)
    if "board_heat_score" in df.columns:
        score = pd.to_numeric(df["board_heat_score"], errors="coerce").fillna(base)
    elif "industry_heat_score" in df.columns or "concept_heat_score" in df.columns:
        industry = _numeric_column(df, "industry_heat_score")
        concept = _numeric_column(df, "concept_heat_score")
        score = pd.concat([industry, concept], axis=1).max(axis=1).fillna(base)
    elif "industry_change_pct" in df.columns:
        change = pd.to_numeric(df["industry_change_pct"], errors="coerce").fillna(0)
        score = base + change * profile["theme_heat_change_slope"]
        if "industry_rank" in df.columns:
            rank = pd.to_numeric(df["industry_rank"], errors="coerce")
            score += (
                (profile["theme_heat_rank_bonus"] - rank.clip(lower=1, upper=10))
                .clip(lower=0)
                .fillna(0)
            )
    else:
        return base.clip(0, 100)

    if "board_heat_trend_score" in df.columns:
        trend = pd.to_numeric(df["board_heat_trend_score"], errors="coerce").fillna(0)
        if "board_heat_observations" in df.columns:
            observations = pd.to_numeric(df["board_heat_observations"], errors="coerce").fillna(0)
        else:
            observations = pd.Series(profile["theme_heat_trend_min_observations"], index=df.index)
        trend_is_reliable = observations >= profile["theme_heat_trend_min_observations"]
        trend_bonus = (trend.clip(lower=0) * profile["theme_heat_trend_slope"]).clip(
            upper=profile["theme_heat_trend_bonus_cap"]
        )
        cooling_penalty = ((-trend).clip(lower=0) * profile["theme_heat_cooling_penalty_slope"]).clip(
            upper=profile["theme_heat_cooling_penalty_cap"]
        )
        score = score + (trend_bonus - cooling_penalty).where(trend_is_reliable, 0)

    if "board_heat_persistence_score" in df.columns:
        persistence = pd.to_numeric(df["board_heat_persistence_score"], errors="coerce").fillna(0)
        persistence_bonus = (
            (persistence - profile["theme_heat_persistence_min_score"]).clip(lower=0)
            * profile["theme_heat_persistence_slope"]
        ).clip(upper=profile["theme_heat_persistence_bonus_cap"])
        score = score + persistence_bonus

    if "board_heat_cooling_score" in df.columns:
        cooling = pd.to_numeric(df["board_heat_cooling_score"], errors="coerce").fillna(0)
        cooling_penalty = (cooling * profile["theme_heat_cooling_score_penalty_slope"]).clip(
            upper=profile["theme_heat_cooling_score_penalty_cap"]
        )
        score = score - cooling_penalty

    overheat = (score - profile["theme_heat_overheat_score"]).clip(lower=0)
    score = score - overheat * profile["theme_heat_overheat_penalty_slope"]
    return score.clip(0, 100)


def _compute_topic_alignment_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    """Score whether industry/concept labels align with hotspot route summaries."""
    base = pd.Series(profile["topic_alignment_unknown_score"], index=df.index)
    if not {"industry", "concepts", "board_heat_summary"} & set(df.columns):
        return base

    scores = []
    for _, row in df.iterrows():
        candidate_topics = _topic_tokens(row.get("industry")) | _topic_tokens(row.get("concepts"))
        route_topics = _topic_tokens(row.get("board_heat_summary"))
        if not candidate_topics or not route_topics:
            scores.append(float(profile["topic_alignment_unknown_score"]))
            continue
        overlap = candidate_topics & route_topics
        score = float(profile["topic_alignment_unknown_score"])
        if overlap:
            score += float(profile["topic_alignment_match_bonus"])
            heat = pd.to_numeric(row.get("board_heat_score"), errors="coerce")
            if pd.notna(heat):
                score += max(float(heat) - 50.0, 0.0) * float(profile["topic_alignment_heat_weight"])
        else:
            score -= float(profile["topic_alignment_unmatched_penalty"])
        scores.append(score)
    return pd.Series(scores, index=df.index).clip(0, 100)


def _compute_old_duck_head_quality_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    """Score how well a candidate matches the Old Duck Head (老鸭头) pattern.

    评分项与形态部位一一对应（阈值来自多源实战共识）：
      鸭颈   barslast_ma5_cross_ma60 落在理想区间（形态成熟度）
      鸭鼻孔 duck_nose_gap_pct 越小越贴合（兼容"死叉派"与"粘合派"两派定义）
      鸭嘴   death_then_golden_5_10（死叉后再金叉）+ coiled_spring_ramp_ratio（放量突破）
      量芝麻 duck_beak_volume_contraction（缩量洗盘，多源共识为峰值/谷值 >= 2）
      风控   ma60_slope_20d_pct（排除 60 线下行的反弹诱多）、
            days_below_ma60_max（排除有效跌破 60 线）、
            duck_head_ma60_gap_pct（鸭头顶应远离 60 日线）

    未形成鸭颈（从未上穿 MA60）或特征列缺失时返回 0 分，避免把普通回调票误判成老鸭头。
    """
    score = pd.Series(float(profile["old_duck_head_quality_base"]), index=df.index)

    # 形态前提：必须已形成鸭颈（MA5 上穿 MA60）。
    if "barslast_ma5_cross_ma60" not in df.columns:
        return pd.Series(0.0, index=df.index)
    bars = pd.to_numeric(df["barslast_ma5_cross_ma60"], errors="coerce")

    # 1. 鸭颈成熟度
    ideal_min = profile["old_duck_head_barslast_ideal_min"]
    ideal_max = profile["old_duck_head_barslast_ideal_max"]
    in_range = bars.between(ideal_min, ideal_max).fillna(False)
    score += in_range * profile["old_duck_head_barslast_bonus"]
    deviation = (
        (ideal_min - bars).clip(lower=0).fillna(0)
        + (bars - ideal_max).clip(lower=0).fillna(0)
    )
    score -= deviation * profile["old_duck_head_barslast_penalty_slope"]

    # 2. 鸭鼻孔贴合度
    if "duck_nose_gap_pct" in df.columns:
        gap = pd.to_numeric(df["duck_nose_gap_pct"], errors="coerce")
        ideal_gap = profile["old_duck_head_nose_gap_ideal"]
        score += (gap <= ideal_gap).fillna(False) * profile["old_duck_head_nose_gap_bonus"]
        score -= (gap - ideal_gap).clip(lower=0).fillna(0) * profile["old_duck_head_nose_gap_penalty_slope"]

    # 3. 鸭嘴确认（死叉后再金叉）
    if "death_then_golden_5_10" in df.columns:
        flag = df["death_then_golden_5_10"].fillna(False).astype(bool)
        score += flag * profile["old_duck_head_death_golden_bonus"]

    # 4. 鸭嘴放量：无量突破属诱多，异常放量易见顶
    if "coiled_spring_ramp_ratio" in df.columns:
        ramp = pd.to_numeric(df["coiled_spring_ramp_ratio"], errors="coerce")
        ramp_min = profile["old_duck_head_ramp_ratio_ideal_min"]
        ramp_max = profile["old_duck_head_ramp_ratio_ideal_max"]
        good = ramp.between(ramp_min, ramp_max).fillna(False)
        score += good * profile["old_duck_head_ramp_ratio_bonus"]
        weak = (ramp_min - ramp).clip(lower=0).fillna(0)
        over = (ramp - ramp_max).clip(lower=0).fillna(0)
        score -= (weak + over) * profile["old_duck_head_ramp_ratio_penalty_slope"]

    # 5. 量芝麻点（缩量洗盘）
    if "duck_beak_volume_contraction" in df.columns:
        contraction = pd.to_numeric(df["duck_beak_volume_contraction"], errors="coerce")
        ideal_c = profile["old_duck_head_volume_contraction_ideal"]
        score += (contraction >= ideal_c).fillna(False) * profile["old_duck_head_volume_contraction_bonus"]
        score -= (ideal_c - contraction).clip(lower=0).fillna(0) * profile["old_duck_head_volume_contraction_penalty_slope"]

    # 6. 60 日均线须走平或向上（下行的惩罚在末尾用乘性衰减处理）
    if "ma60_slope_20d_pct" in df.columns:
        slope = pd.to_numeric(df["ma60_slope_20d_pct"], errors="coerce")
        slope_min = profile["old_duck_head_ma60_slope_min"]
        score += (slope >= slope_min).fillna(False) * profile["old_duck_head_ma60_slope_bonus"]

    # 7. 鸭头顶应远离 60 日线
    if "duck_head_ma60_gap_pct" in df.columns:
        head_gap = pd.to_numeric(df["duck_head_ma60_gap_pct"], errors="coerce")
        ideal_head = profile["old_duck_head_head_gap_ideal_min"]
        score += (head_gap >= ideal_head).fillna(False) * profile["old_duck_head_head_gap_bonus"]
        score -= (ideal_head - head_gap).clip(lower=0).fillna(0) * profile["old_duck_head_head_gap_penalty_slope"]

    # 8. 未破位加分（有效跌破的惩罚在末尾用乘性衰减处理）
    if "days_below_ma60_max" in df.columns:
        below = pd.to_numeric(df["days_below_ma60_max"], errors="coerce")
        score += (below.fillna(1.0) <= 0.0) * profile["old_duck_head_days_below_ma60_bonus"]

    # 9. 追高惩罚
    if "change_20d" in df.columns:
        chase = pd.to_numeric(df["change_20d"], errors="coerce")
        start = profile["old_duck_head_chase_start_pct"]
        score -= (chase - start).clip(lower=0).fillna(0) * profile["old_duck_head_chase_penalty_slope"]

    # 10. 关键风控采用乘性衰减：线性惩罚会被 clip(0, 100) 上限掩盖
    #     （实测：60 线下行 / 有效跌破 60 线 / 鸭嘴无量突破在线性惩罚下仍得满分）。
    if "ma60_slope_20d_pct" in df.columns:
        slope = pd.to_numeric(df["ma60_slope_20d_pct"], errors="coerce")
        bad_slope = (slope < profile["old_duck_head_ma60_slope_min"]).fillna(False)
        score = score.where(~bad_slope, score * profile["old_duck_head_ma60_slope_decay"])

    if "days_below_ma60_max" in df.columns:
        below = pd.to_numeric(df["days_below_ma60_max"], errors="coerce")
        bad_below = (below > profile["old_duck_head_days_below_ma60_max_allowed"]).fillna(False)
        score = score.where(~bad_below, score * profile["old_duck_head_days_below_ma60_decay"])

    if "coiled_spring_ramp_ratio" in df.columns:
        ramp = pd.to_numeric(df["coiled_spring_ramp_ratio"], errors="coerce")
        bad_ramp = (ramp < profile["old_duck_head_ramp_ratio_weak"]).fillna(False)
        score = score.where(~bad_ramp, score * profile["old_duck_head_ramp_weak_decay"])

    # 11. 形态前提门禁：从未形成鸭颈（MA5 未上穿 MA60）一律 0 分。
    if "barslast_ma5_cross_ma60" in df.columns:
        bars = pd.to_numeric(df["barslast_ma5_cross_ma60"], errors="coerce")
        score = score.where(bars.notna(), 0.0)

    return score.clip(0, 100)


def _compute_consolidation_quality_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    """Score how well a candidate matches the consolidation-breakout pattern.

    Rewards tight/long consolidation, gentle volume expansion, MACD bullishness,
    proximity to 20-day highs, and spring-compression patterns.
    Penalizes excessive range, volatility, and late chasing.
    """
    score = pd.Series(float(profile["consolidation_quality_base"]), index=df.index)

    # 1. Consolidation duration: longer is better once the minimum is met.
    # Penalize only stocks that have not consolidated enough; reward marginal
    # extra duration to reflect the strategy's rule that "the longer the base,
    # the more reliable the breakout".
    if "consolidation_days_20d" in df.columns:
        days = pd.to_numeric(df["consolidation_days_20d"], errors="coerce").fillna(0)
        ideal = profile["consolidation_quality_days_ideal"]
        slope = profile["consolidation_quality_days_slope"]
        shortfall = (ideal - days).clip(lower=0)
        score -= shortfall * slope
        score += (days - ideal).clip(lower=0) * (slope * 0.25)

    # 2. Consolidation range: prefer a tight but not flat range.
    if "range_20d_pct" in df.columns:
        range_ = pd.to_numeric(df["range_20d_pct"], errors="coerce")
        ideal_min = profile["consolidation_quality_range_ideal_min"]
        ideal_max = profile["consolidation_quality_range_ideal_max"]
        slope = profile["consolidation_quality_range_penalty_slope"]
        penalty = (
            (range_ - ideal_max).clip(lower=0) + (ideal_min - range_).clip(lower=0)
        ) * slope
        score -= penalty.fillna(0)

    # 3. Volatility: lower is better for a clean base.
    if "volatility_20d_pct" in df.columns:
        vol = pd.to_numeric(df["volatility_20d_pct"], errors="coerce")
        ideal = profile["consolidation_quality_volatility_ideal"]
        slope = profile["consolidation_quality_volatility_penalty_slope"]
        score -= (vol - ideal).clip(lower=0).fillna(0) * slope

    # 4. Breakout / near-high bonus.
    if "breakout_20d_pct" in df.columns:
        breakout = pd.to_numeric(df["breakout_20d_pct"], errors="coerce").fillna(-100)
        near_high = breakout >= profile["consolidation_quality_near_high_threshold_pct"]
        score += near_high.astype(float) * profile["consolidation_quality_near_high_penalty"]
        score += breakout.clip(lower=0) * profile["consolidation_quality_breakout_bonus_slope"]

    # 5. Momentum penalty: avoid chasing stocks that have already moved too far.
    if "change_10d" in df.columns:
        c10 = pd.to_numeric(df["change_10d"], errors="coerce").fillna(0)
        score -= (
            c10 - profile["consolidation_quality_momentum_10d_threshold_pct"]
        ).clip(lower=0) * profile["consolidation_quality_momentum_10d_penalty_slope"]
    if "change_20d" in df.columns:
        c20 = pd.to_numeric(df["change_20d"], errors="coerce").fillna(0)
        score -= (
            c20 - profile["consolidation_quality_momentum_20d_threshold_pct"]
        ).clip(lower=0) * profile["consolidation_quality_momentum_20d_penalty_slope"]

    # 6. Volume expansion: reward today's volume pick-up and recent volume ramp.
    if "volume_expand_1d" in df.columns:
        ve1 = pd.to_numeric(df["volume_expand_1d"], errors="coerce").fillna(1.0)
        threshold = profile["consolidation_quality_volume_expand_threshold"]
        score += (ve1 - threshold).clip(lower=0) * profile["consolidation_quality_volume_expand_bonus"]
    if "volume_expand_5d" in df.columns:
        ve5 = pd.to_numeric(df["volume_expand_5d"], errors="coerce").fillna(1.0)
        threshold = profile["consolidation_quality_volume_expand_threshold"]
        score += (ve5 - threshold).clip(lower=0) * (profile["consolidation_quality_volume_expand_bonus"] * 0.5)

    # 7. Consecutive volume spikes: reward sustained demand.
    if "consecutive_volume_spike_3d" in df.columns:
        spike3 = df["consecutive_volume_spike_3d"].fillna(False).astype(bool)
        score += spike3.astype(float) * profile["consolidation_quality_consecutive_spike_3d_bonus"]
    if "consecutive_volume_spike_2d" in df.columns:
        spike2 = df["consecutive_volume_spike_2d"].fillna(False).astype(bool)
        score += spike2.astype(float) * profile["consolidation_quality_consecutive_spike_2d_bonus"]

    # 8. Spring compression: reward contraction followed by controlled expansion.
    if {"coiled_spring_contraction_pct", "coiled_spring_ramp_ratio"} <= set(df.columns):
        contraction = pd.to_numeric(df["coiled_spring_contraction_pct"], errors="coerce").fillna(0)
        ramp = pd.to_numeric(df["coiled_spring_ramp_ratio"], errors="coerce").fillna(1.0)
        is_spring = (
            (contraction >= profile["consolidation_quality_coiled_spring_contraction_min"])
            & (ramp >= profile["consolidation_quality_coiled_spring_ramp_min"])
            & (ramp <= profile["consolidation_quality_coiled_spring_ramp_max"])
        )
        score += is_spring.astype(float) * profile["consolidation_quality_coiled_spring_bonus"]

    # 9. Longer-base bonus: stocks that have consolidated across 60/120 days.
    if "consolidation_days_60d" in df.columns:
        c60 = pd.to_numeric(df["consolidation_days_60d"], errors="coerce").fillna(0)
        score += (c60 > 0).astype(float) * profile["consolidation_quality_long_bonus_60d"]
    if "consolidation_days_120d" in df.columns:
        c120 = pd.to_numeric(df["consolidation_days_120d"], errors="coerce").fillna(0)
        score += (c120 > 0).astype(float) * profile["consolidation_quality_long_bonus_120d"]
    if "consolidation_days_250d" in df.columns:
        c250 = pd.to_numeric(df["consolidation_days_250d"], errors="coerce").fillna(0)
        score += (c250 > 0).astype(float) * profile["consolidation_quality_long_bonus_250d"]

    # 10. MA structure bonus.
    ma_bullish_bonus = profile.get("consolidation_quality_ma_bullish_bonus", 5.0)
    price_above_ma20_bonus = profile.get("consolidation_quality_price_above_ma20_bonus", 3.0)
    if "ma_bullish" in df.columns:
        score += df["ma_bullish"].fillna(False).astype(bool).astype(float) * ma_bullish_bonus
    if "price_above_ma20" in df.columns:
        score += df["price_above_ma20"].fillna(False).astype(bool).astype(float) * price_above_ma20_bonus

    # 11. 破位惩罚：大阴线 + 倍量柱 + 下穿多条均线 = 出货/破位，与横盘蓄势相悖（如「天银机电」式）。
    bear_penalty = _compute_bearish_breakdown_penalty(df, profile, "consolidation_quality_bear_")
    score = score - bear_penalty

    # 12. 累积破位惩罚：当日形态未触发破位但近 N 日已下行 / 主力净流出的票也应减分。
    cum_bear = _compute_cumulative_breakdown_penalty(
        df, profile, "consolidation_quality_cum_"
    )
    score = score - cum_bear

    # 12.5 主升回落惩罚：近 20 日最大回撤超出阈值部分按斜率减分。
    #      用于压制「刚主升后又回落」的 V 形（如冲高到 85.8 再下来）——这类票
    #      在旧专用评分器里会被当成"连续蓄势"而拿满分，与"横盘很久才突破"语义相悖。
    #      注意 max_drawdown_20d_pct 为负值约定（回撤越大越负），故取绝对值比较。
    if "max_drawdown_20d_pct" in df.columns:
        _dd = pd.to_numeric(df["max_drawdown_20d_pct"], errors="coerce")
        _dd_threshold = float(
            profile.get("consolidation_quality_surge_pullback_drawdown_threshold_pct", 8.0)
        )
        _dd_slope = float(
            profile.get("consolidation_quality_surge_pullback_penalty_slope", 3.0)
        )
        _dd_excess = (_dd.abs() - _dd_threshold).clip(lower=0.0)
        score = score - _dd_excess.fillna(0.0) * _dd_slope

    # --- Money Flow Confirmation (0-10 pts, consolidation-breakout overlay) ---
    #     Rewards sustained main-force accumulation; penalises outflow.
    mf_score = pd.Series(5.0, index=df.index)
    if "mf_available" in df.columns:
        mf_avail = df["mf_available"].fillna(False).astype(bool)
        mf_norm_min = profile.get("consolidation_mf_inflow_5d_min", 300)
        mf_norm_max = profile.get("consolidation_mf_inflow_5d_max", 3000)
        mf_outflow_min = profile.get("consolidation_mf_outflow_5d_min", -300)
        mf_outflow_max = profile.get("consolidation_mf_outflow_5d_max", -3000)
        if "mf_net_inflow_5d" in df.columns:
            inflow5d = pd.to_numeric(df["mf_net_inflow_5d"], errors="coerce").fillna(0.0)
            # reward positive inflow
            mf_score = mf_score + (
                mf_avail & (inflow5d > mf_norm_min)
            ).astype(float) * ((inflow5d - mf_norm_min) / max(mf_norm_max - mf_norm_min, 1.0)).clip(
                lower=0, upper=1.0
            ) * 5.0
            # penalise outflow (up to -3 pts)
            outflow_mask = mf_avail & (inflow5d < 0)
            outflow_ratio = ((inflow5d - mf_outflow_min) / max(mf_outflow_max - mf_outflow_min, 1.0)).clip(
                lower=0, upper=1.0
            )
            mf_score = mf_score - outflow_mask.astype(float) * outflow_ratio * 3.0
        if "mf_consecutive_days" in df.columns:
            cons = pd.to_numeric(df["mf_consecutive_days"], errors="coerce").fillna(0).clip(lower=0)
            mf_score = mf_score + (mf_avail & (cons >= 3)).astype(float) * 3.0
        if "mf_inflow_strength_pct" in df.columns:
            strength = pd.to_numeric(df["mf_inflow_strength_pct"], errors="coerce").fillna(0.0)
            # reward positive strength
            mf_score = mf_score + (mf_avail & (strength > 2.0)).astype(float) * ((strength - 2.0) / 10.0).clip(lower=0, upper=1.0) * 2.0
            # penalise negative strength (up to -2 pts)
            neg_strength_mask = mf_avail & (strength < 0)
            neg_strength_ratio = (strength / -10.0).clip(lower=0, upper=1.0)
            mf_score = mf_score - neg_strength_mask.astype(float) * neg_strength_ratio * 2.0
    mf_score = mf_score.clip(lower=0, upper=10.0)
    score = score + mf_score

    return score.clip(0, 100)


def _topic_tokens(value: object) -> set[str]:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return set()
    normalized = text
    for sep in ["|", ",", "，", ";", "；", "/"]:
        normalized = normalized.replace(sep, " ")
    tokens = set()
    for raw in normalized.split():
        token = raw.split(":", 1)[0].strip()
        if token and token.lower() not in {"rank", "nan", "none", "<na>"}:
            tokens.add(token)
    return tokens


def _numeric_column(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(pd.NA, index=df.index)
    return pd.to_numeric(df[column], errors="coerce")


def _compute_bearish_breakdown_penalty(
    df: pd.DataFrame, profile: dict[str, float], prefix: str
) -> pd.Series:
    """破位惩罚：大阴线 + 倍量柱 + 下穿多条均线 = 出货/破位信号，与吸筹/横盘蓄势逻辑相悖。

    三个条件全部命中时给予最强扣分；部分命中按比例扣减（每个条件独立扣分 + 全中额外加成）。
    用于 bottom_accumulation / consolidation 等底部吸筹类策略。

    prefix 用于在各策略 profile 中区分阈值/权重，例如：
      - bottom_accumulation_bear_candle_pct / _bear_volume_ratio / _bear_ma_breakdown_min / _bear_penalty_each / _bear_penalty_all_extra / _bear_penalty_cap
      - consolidation_quality_bear_candle_pct / ... 同形
    """
    cfg = {
        "candle_pct": float(profile.get(f"{prefix}candle_pct", -5.0)),       # 当日涨跌幅 ≤ 此值视为大阴线
        "volume_ratio": float(profile.get(f"{prefix}volume_ratio", 2.0)),    # 量比 ≥ 此值视为倍量柱
        "ma_breakdown_min": float(profile.get(f"{prefix}ma_breakdown_min", 2)),
        "penalty_each": float(profile.get(f"{prefix}penalty_each", 6.0)),
        "penalty_all_extra": float(profile.get(f"{prefix}penalty_all_extra", 7.0)),
        "penalty_cap": float(profile.get(f"{prefix}penalty_cap", 25.0)),
    }

    change_pct = _numeric_column(df, "change_pct")
    volume_ratio = _numeric_column(df, "volume_ratio")
    ma_breakdown = _numeric_column(df, "ma_breakdown_count")

    # 大阴线：当日跌幅超过阈值（candle_pct 为负，change_pct 更负即满足）
    bear_candle = change_pct <= cfg["candle_pct"]
    # 倍量柱：量比（今日量 / 近5日均量）≥ 阈值
    double_volume = volume_ratio >= cfg["volume_ratio"]
    # 下穿多条均线：收盘价低于 ≥ min 条均线
    ma_break = ma_breakdown >= cfg["ma_breakdown_min"]

    penalty = (
        bear_candle.astype(float) * cfg["penalty_each"]
        + double_volume.astype(float) * cfg["penalty_each"]
        + ma_break.astype(float) * cfg["penalty_each"]
    )
    all_three = bear_candle & double_volume & ma_break
    penalty = penalty + all_three.astype(float) * cfg["penalty_all_extra"]
    return penalty.clip(lower=0, upper=cfg["penalty_cap"])


def _rank_score(
    series: pd.Series,
    *,
    lower_is_better: bool,
    na_score: float = 50.0,
) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() == 0:
        return pd.Series(float(na_score), index=series.index)

    ranks = numeric.rank(
        ascending=not lower_is_better,
        na_option="keep",
        pct=True,
    ) * 100
    return ranks.fillna(float(na_score)).clip(0, 100)


def _compute_bottom_accumulation_quality_score(df: pd.DataFrame, profile: dict[str, float]) -> pd.Series:
    """Score how well a candidate matches the bottom-accumulation (底部吸筹) pattern.

    Bottom accumulation describes stocks that have experienced significant
    decline and are now showing institutional accumulation signals:

    1. **Decline Depth** (0-25 pts): 60d decline in sweet-spot range
       (15-40%). Too shallow = no real bottom; too deep = breakdown risk.

    2. **RSI Recovery** (0-20 pts): RSI was oversold (<35) and now recovering.
       Stronger recovery = more points.

    3. **Volume Accumulation** (0-18 pts): Moderate expansion (温和放量)
       confirming quiet accumulation at the bottom.  Sweet spot 1.1-1.6x;
       explosive volume (>2x) penalized to exclude limit-up chasers.

    4. **MACD Bottom Structure** (0-15 pts): MACD improving or golden cross
       in negative territory (bottom context).

    5. **Price Stabilization** (0-20 pts): Price bounced off 60d low and
       is stabilizing (MA5 turning up). **Hump-shaped curve**: 3-8% = ideal
       (ramps to 10), 8-15% = diminishing (10→5), 15-20% = tapering (5→0),
       >20% = 0 — penalises stocks that have already run far from the bottom.

    6. **Money Flow Confirmation** (0-15 pts): Main-force net inflow from
       Tushare moneyflow_dc (→AkShare→efinance fallback). Rewards 5d
       cumulative inflow amount, consecutive inflow days, and inflow
       strength as % of turnover. No data → neutral 7 pts.

    7. **Chase Penalty** (0 to -20 pts): Stock has already run up too much
       from the bottom (change_20d > 8% → -0..-15, change_10d > 5% →
       up to -5 more). Reduces score for stocks that have left the bottom
       zone. Hard filter change_20d_max=20% still applies.

    8. **Upper Shadow Risk** (0 to -10 pts): Long upper shadow (>50% of
       daily range) combined with recent rise (>3% 10d) is a bearish
       reversal signal (长上影线出货). Penalised only when both conditions
       co-occur.

    Total: 0-100. Each sub-score contributes additively;
           factors 7 & 8 are penalty-only (≤0).
    """
    score = pd.Series(0.0, index=df.index)

    # --- 1. Decline Depth (0-25 points) ---
    if "change_60d" in df.columns:
        c60 = pd.to_numeric(df["change_60d"], errors="coerce").fillna(0)
        sweet_min = profile.get("bottom_accumulation_decline_sweet_min", 15.0)
        sweet_max = profile.get("bottom_accumulation_decline_sweet_max", 40.0)
        too_deep = profile.get("bottom_accumulation_decline_too_deep_max", 60.0)

        decline = (-c60).clip(lower=0)

        # Piecewise linear:
        #   0–5%: 0 pts (trivial decline → no bottom)
        #   5–15%: ramp 0 → 25
        #   15–40%: full 25 pts (sweet spot)
        #   40–60%: ramp 25 → 0
        #   >60%: 0 pts (too risky / breakdown)
        deep_slope = 25.0 / max(too_deep - sweet_max, 1.0)
        shallow_slope = 25.0 / max(sweet_min - 5.0, 1.0)

        decline_score = pd.Series(0.0, index=df.index)
        decline_score = decline_score.where(
            (decline < 5.0) | (decline > too_deep), 0.0
        )
        # 5–15% ramp up
        mask_shallow = (decline >= 5.0) & (decline < sweet_min)
        decline_score = decline_score.where(
            ~mask_shallow, (decline - 5.0) * shallow_slope
        )
        # 15–40% full
        mask_sweet = (decline >= sweet_min) & (decline <= sweet_max)
        decline_score = decline_score.where(~mask_sweet, 25.0)
        # 40–60% ramp down
        mask_deep = (decline > sweet_max) & (decline <= too_deep)
        decline_score = decline_score.where(
            ~mask_deep, 25.0 - (decline - sweet_max) * deep_slope
        )
        decline_score = decline_score.clip(lower=0, upper=25.0)
        score = score + decline_score
    else:
        score = score + 10.0  # neutral

    # --- 2. RSI Oversold Recovery (0-20 points) ---
    if "rsi_oversold_20d" in df.columns and "rsi_recovery_pct" in df.columns:
        rsi_oversold = df["rsi_oversold_20d"].astype(bool)
        rsi_rec = pd.to_numeric(df["rsi_recovery_pct"], errors="coerce").fillna(0)

        rsi_score = rsi_oversold.astype(float) * 5.0
        rec_bonus = (rsi_rec / 20.0).clip(lower=0, upper=1.0) * 15.0
        rsi_score = (rsi_score + rec_bonus).clip(lower=0, upper=20.0)
        score = score + rsi_score
    else:
        score = score + 5.0

    # --- 3. Volume Accumulation (0-18 points, moderate-expansion preference) ---
    #     Favor 温和放量 (moderate volume expansion) as confirmation of quiet
    #     accumulation. Penalize 爆量 (explosive volume) which correlates with
    #     chase chasers and limit-up stocks that have already run.
    vol_score = pd.Series(3.0, index=df.index)

    # 3a. Volume expansion ratio (放量): sweet spot 1.1-1.6x over 20d avg.
    #     Below 1.1: negligible; 1.1-1.6: ideal bottom accumulation;
    #     beyond 1.6: diminishing, risk of hot-money chase; >2.0: penalty.
    if "volume_expand_5d" in df.columns:
        ve5 = pd.to_numeric(df["volume_expand_5d"], errors="coerce").fillna(1.0)
        # mild zone (1.0-1.3): gradual ramp +0..+3
        vol_score = vol_score + ((ve5 - 1.0) / 0.3).clip(lower=0, upper=1.0) * 3.0
        # ideal zone (1.3-1.6): peak bonus +3..+5, total 3+5=8 for ideal
        vol_score = vol_score + ((ve5 - 1.3) / 0.3).clip(lower=0, upper=1.0) * 2.0
        # cooling zone (1.6-2.0): give back from peak, down to +0 at >2.0
        vol_score = vol_score - ((ve5 - 1.6) / 0.4).clip(lower=0, upper=1.0) * 5.0

    # 3b. Consecutive volume spike days (连续放量天数):
    #     2-4 days = sustained quiet buying (good); 5+ days = too hot.
    if "consecutive_volume_spike_days" in df.columns:
        spike_days = pd.to_numeric(df["consecutive_volume_spike_days"], errors="coerce").fillna(0).clip(lower=0)
        vol_score = vol_score + ((spike_days >= 2) & (spike_days <= 4)).astype(float) * 3.0

    # 3c. 倍量 bonus (volume_ratio_20d): sweet spot 1.1-1.5x.
    #     Mild above-baseline = good; >2.0x = explosive chase → penalty.
    if "volume_ratio_20d" in df.columns:
        vr20 = pd.to_numeric(df["volume_ratio_20d"], errors="coerce").fillna(1.0)
        vol_score = vol_score + ((vr20 > 1.1) & (vr20 <= 1.3)).astype(float) * 2.0
        vol_score = vol_score + ((vr20 > 1.3) & (vr20 <= 1.5)).astype(float) * 3.0
        vol_score = vol_score + ((vr20 > 1.5) & (vr20 <= 2.0)).astype(float) * 1.0
        vol_score = vol_score - (vr20 > 2.0).astype(float) * 2.0

    vol_score = vol_score.clip(lower=0, upper=18.0)
    score = score + vol_score

    # --- 4. MACD Bottom Structure (0-15 points) ---
    if "macd_bottom_cross" in df.columns:
        macd_cross = df["macd_bottom_cross"].astype(bool)
        macd_score = macd_cross.astype(float) * 15.0
        score = score + macd_score
    else:
        score = score + 5.0

    # --- 5. Price Stabilization (0-20 points, hump-shaped) ---
    # Hump-shaped: sweet spot is 3-8% above 60d low — confirmed bottom
    # bounce but still near accumulation zone. Beyond 8% the stock has
    # already run and the bonus declines. >20% = 0 (not a bottom stock).
    stab_score = pd.Series(5.0, index=df.index)

    if "price_vs_60d_low_pct" in df.columns:
        p_low = pd.to_numeric(df["price_vs_60d_low_pct"], errors="coerce").fillna(0)

        low_bonus = pd.Series(0.0, index=df.index)

        # 3-8%: ideal near-bottom zone → ramp 0 → 10
        mask_ideal = (p_low >= 3.0) & (p_low < 8.0)
        low_bonus = low_bonus.where(
            ~mask_ideal, (p_low - 3.0) / 5.0 * 10.0
        )

        # 8-15%: leaving the bottom → decline 10 → 5
        mask_diminish = (p_low >= 8.0) & (p_low < 15.0)
        low_bonus = low_bonus.where(
            ~mask_diminish, 10.0 - (p_low - 8.0) / 7.0 * 5.0
        )

        # 15-20%: too far from bottom → decline 5 → 0
        mask_taper = (p_low >= 15.0) & (p_low < 20.0)
        low_bonus = low_bonus.where(
            ~mask_taper, 5.0 - (p_low - 15.0) / 5.0 * 5.0
        )

        # >20%: 0 (not a bottom stock; price has already run away)
        # <3%: 0 (too weak bounce)

        low_bonus = low_bonus.clip(lower=0, upper=10.0)
        stab_score = stab_score + low_bonus

    if "ma5_turn_up_pct" in df.columns:
        ma5_turn = pd.to_numeric(df["ma5_turn_up_pct"], errors="coerce").fillna(0)
        turn_min = profile.get("bottom_accumulation_ma5_turn_up_min", 1.0)
        turn_bonus = ((ma5_turn - (-2.0)) / (turn_min + 2.0)).clip(lower=0, upper=1.0) * 5.0
        stab_score = stab_score + turn_bonus

    stab_score = stab_score.clip(lower=0, upper=20.0)
    score = score + stab_score

    # --- 6. Money Flow Confirmation (0-15 points) ---
    #     Tushare moneyflow_dc → AkShare → efinance fallback chain.
    #     Rewards names with rising main-force stakes: inflow amount,
    #     inflow strength as % of turnover, and consecutive days.
    #     No data available → neutral 7 pts (not penalized).
    #     Negative main-force flow is penalised because outflow contradicts the
    #     bottom-accumulation thesis.
    mf_score = pd.Series(7.0, index=df.index)

    if "mf_available" in df.columns:
        mf_avail = df["mf_available"].fillna(False).astype(bool)

        # 6a. 5-day cumulative net inflow (万元): positive & large → rewarded;
        #     negative → penalised.
        inflow_min = profile.get("bottom_accumulation_mf_inflow_5d_min", 500)
        inflow_max = profile.get("bottom_accumulation_mf_inflow_5d_max", 5000)
        outflow_min = profile.get("bottom_accumulation_mf_outflow_5d_min", -500)
        outflow_max = profile.get("bottom_accumulation_mf_outflow_5d_max", -5000)
        if "mf_net_inflow_5d" in df.columns:
            inflow5d = pd.to_numeric(df["mf_net_inflow_5d"], errors="coerce").fillna(0.0)
            # reward positive inflow
            mf_score = mf_score + (
                mf_avail & (inflow5d > inflow_min)
            ).astype(float) * ((inflow5d - inflow_min) / max(inflow_max - inflow_min, 1.0)).clip(
                lower=0, upper=1.0
            ) * 6.0
            # penalise outflow (milder than reward: up to -4 pts)
            outflow_mask = mf_avail & (inflow5d < 0)
            outflow_range = outflow_min - outflow_max
            outflow_ratio = (
                (outflow_min - inflow5d) / max(outflow_range, 1.0)
            ).clip(lower=0, upper=1.0)
            mf_score = mf_score - outflow_mask.astype(float) * outflow_ratio * 4.0

        # 6b. Consecutive positive inflow days:
        #     1-2 days → mild interest, 3-4 → sustained, 5+ → strong commitment
        #     Consecutive outflow days → penalty.
        if "mf_consecutive_days" in df.columns:
            cons = pd.to_numeric(df["mf_consecutive_days"], errors="coerce").fillna(0).clip(lower=0)
            mf_score = mf_score + (mf_avail & (cons >= 2) & (cons < 4)).astype(float) * 3.0
            mf_score = mf_score + (mf_avail & (cons >= 4)).astype(float) * 5.0

        # 6c. Inflow strength (% of turnover): higher % → stronger conviction;
        #     negative strength → penalised.
        strength_min = profile.get("bottom_accumulation_mf_strength_pct_min", 2.0)
        strength_max = profile.get("bottom_accumulation_mf_strength_pct_max", 10.0)
        if "mf_inflow_strength_pct" in df.columns:
            strength = pd.to_numeric(df["mf_inflow_strength_pct"], errors="coerce").fillna(0.0)
            # reward positive strength
            mf_score = mf_score + (
                mf_avail & (strength > strength_min)
            ).astype(float) * ((strength - strength_min) / max(strength_max - strength_min, 1.0)).clip(
                lower=0, upper=1.0
            ) * 4.0
            # penalise negative strength (up to -3 pts)
            neg_strength_mask = mf_avail & (strength < 0)
            neg_strength_ratio = (strength / -10.0).clip(lower=0, upper=1.0)
            mf_score = mf_score - neg_strength_mask.astype(float) * neg_strength_ratio * 3.0

    mf_score = mf_score.clip(lower=0, upper=15.0)
    score = score + mf_score

    # --- 7. Chase Penalty (0 to -20 points) ---
    # Stocks that have already run up significantly from the bottom are no
    # longer good accumulation candidates. Uses 20d momentum (primary) and
    # 10d momentum (secondary, catches recent sprint).
    chase_penalty = pd.Series(0.0, index=df.index)

    chase_20d_start = profile.get("bottom_accumulation_chase_20d_start_pct", 8.0)
    chase_20d_max = profile.get("bottom_accumulation_chase_20d_max_pct", 20.0)
    chase_10d_start = profile.get("bottom_accumulation_chase_10d_start_pct", 5.0)

    if "change_20d" in df.columns:
        c20 = pd.to_numeric(df["change_20d"], errors="coerce").fillna(0)
        # 8-15%: linear penalty 0 → -10
        c20_early = ((c20 - chase_20d_start) / (15.0 - chase_20d_start)).clip(
            lower=0, upper=1.0
        ) * (-10.0)
        # 15-20%: additional penalty -10 → -15
        c20_late = ((c20 - 15.0) / max(chase_20d_max - 15.0, 1.0)).clip(
            lower=0, upper=1.0
        ) * (-5.0)
        chase_penalty = chase_penalty + c20_early + c20_late

    if "change_10d" in df.columns:
        c10 = pd.to_numeric(df["change_10d"], errors="coerce").fillna(0)
        # 5-10%: recent sprint penalty 0 → -5
        c10_penalty = ((c10 - chase_10d_start) / 5.0).clip(
            lower=0, upper=1.0
        ) * (-5.0)
        chase_penalty = chase_penalty + c10_penalty

    chase_penalty = chase_penalty.clip(lower=-20.0, upper=0.0)
    score = score + chase_penalty

    # --- 8. Upper Shadow Risk Penalty (0 to -10 points) ---
    # Long upper shadow (长上影) after a rise = bearish reversal / profit-
    # taking signal. Penalised only when shadow >50% of daily range AND
    # the stock has risen >3% in 10d (confirms it's a top-of-move shadow).
    shadow_penalty = pd.Series(0.0, index=df.index)

    shadow_threshold = profile.get(
        "bottom_accumulation_upper_shadow_threshold_pct", 50.0
    )
    shadow_rise_min = profile.get(
        "bottom_accumulation_upper_shadow_rise_min_pct", 3.0
    )

    if "upper_shadow_pct" in df.columns and "change_10d" in df.columns:
        us = pd.to_numeric(df["upper_shadow_pct"], errors="coerce").fillna(0)
        c10 = pd.to_numeric(df["change_10d"], errors="coerce").fillna(0)

        long_shadow = us > shadow_threshold
        rising = c10 > shadow_rise_min
        trigger = long_shadow & rising

        # Shadow severity: 50-100% → linear penalty 0 → -10
        shadow_severity = (
            (us - shadow_threshold) / max(100.0 - shadow_threshold, 1.0)
        ).clip(lower=0, upper=1.0)
        shadow_penalty = shadow_penalty + trigger.astype(float) * shadow_severity * (-10.0)

    shadow_penalty = shadow_penalty.clip(lower=-10.0, upper=0.0)
    score = score + shadow_penalty

    # --- 9. 破位惩罚（0 至 -25 分）---
    # 大阴线 + 倍量柱 + 下穿多条均线 = 出货/破位信号（如「天银机电」式放量大跌击穿均线），
    # 与底部吸筹逻辑相悖，应作为强减分项。三条件全中时扣分最强。
    bear_penalty = _compute_bearish_breakdown_penalty(df, profile, "bottom_accumulation_bear_")
    score = score - bear_penalty

    # --- 10. 累积破位惩罚（0 至 -cum_penalty_cap 分）---
    # 当日形态可能只是缩量回踩，但若近 20 日 / 60 日已经处于"持续下行 + 主力净流出"，
    # 也不应被识别为吸筹。该维度补充"当日未触发破位但多日已破位"的情况。
    cum_bear = _compute_cumulative_breakdown_penalty(
        df, profile, "bottom_accumulation_cum_"
    )
    score = score - cum_bear

    return score.clip(lower=0, upper=100.0)


def _compute_cumulative_breakdown_penalty(
    df: pd.DataFrame, profile: dict[str, float], prefix: str
) -> pd.Series:
    """补齐"累积破位"维度（只用已有列），与 _compute_bearish_breakdown_penalty 互补。

    三个互相独立的累积信号，每个命中扣 `prefix_penalty_each` 分：
      1. mf_negative_5d：mf_net_inflow_5d < `prefix_mf_negative_5d_threshold`（默认 0）
         → 5 日主力资金累计净流出，与"底部吸筹 / 横盘蓄势"逻辑相悖。
      2. prolonged_decline：change_20d <= `prefix_prolonged_decline_20d_pct`（默认 -15%）
         且当日 change_pct <= 0 → 持续大幅回撤且今日仍跌。
      3. deep_break：change_60d < `prefix_deep_break_60d_pct`（默认 -50%）
         → 60 日已深跌破底（不是吸筹的 sweet spot，而是趋势性破位）。
      4.【主信号，强烈】ma_breakdown_strong：ma_breakdown_count >= `prefix_strong_ma_breakdown_min`
         且 change_5d <= `prefix_strong_prolonged_decline_5d_pct`（默认 -5%）且当日
         change_pct <= 0 → "均线已发散 + 5 日持续下行 + 当日仍跌"的结构性破位
         （如立昂技术式「多日主力流出 + 跌破多根均线 + 持续阴跌」）。
         该信号单独扣 `prefix_strong_penalty`（默认 14），与弱信号叠加，
         共同受 `prefix_penalty_cap`（默认 20）封顶。

    最终 clip 到 [0, `prefix_penalty_cap`]，避免过分扣分。
    所有信号在数据缺失（NaN）时按"未触发"处理，避免误伤上游数据不全的票。
    """
    penalty_each = float(profile.get(f"{prefix}penalty_each", 4.0))
    penalty_cap = float(profile.get(f"{prefix}penalty_cap", 20.0))

    if penalty_each <= 0 or penalty_cap <= 0:
        # 配置关闭累积破位（任意参数 ≤ 0 视为禁用）
        return pd.Series(0.0, index=df.index)

    score = pd.Series(0.0, index=df.index)

    # 1. 5 日主力累计净流出
    if "mf_net_inflow_5d" in df.columns:
        inflow5d = pd.to_numeric(df["mf_net_inflow_5d"], errors="coerce")
        threshold = float(profile.get(f"{prefix}mf_negative_5d_threshold", 0.0))
        mf_negative_mask = inflow5d.notna() & (inflow5d < threshold)
        score = score + mf_negative_mask.astype(float) * penalty_each

    # 2. 持续大幅回撤且今日仍跌
    decline_threshold = float(profile.get(f"{prefix}prolonged_decline_20d_pct", -15.0))
    if "change_20d" in df.columns and "change_pct" in df.columns:
        c20 = pd.to_numeric(df["change_20d"], errors="coerce")
        c0 = pd.to_numeric(df["change_pct"], errors="coerce")
        prolonged_mask = c20.notna() & c0.notna() & (c20 <= decline_threshold) & (c0 <= 0)
        score = score + prolonged_mask.astype(float) * penalty_each

    # 3. 60 日深跌破底（非吸筹的 sweet spot，而是趋势性破位）
    deep_threshold = float(profile.get(f"{prefix}deep_break_60d_pct", -50.0))
    if "change_60d" in df.columns:
        c60 = pd.to_numeric(df["change_60d"], errors="coerce")
        deep_mask = c60.notna() & (c60 < deep_threshold)
        score = score + deep_mask.astype(float) * penalty_each

    # 4. 强信号组合：均线已发散 + 5 日持续下行 + 当日仍跌。
    # 仅当 ma_breakdown_count / change_5d / change_pct 三列都齐备且非 NaN 时触发。
    if {"ma_breakdown_count", "change_5d", "change_pct"}.issubset(df.columns):
        ma_break_min = int(profile.get(f"{prefix}strong_ma_breakdown_min", 2))
        decline_5d_threshold = float(
            profile.get(f"{prefix}strong_prolonged_decline_5d_pct", -5.0)
        )
        strong_penalty = float(profile.get(f"{prefix}strong_penalty", 14.0))

        ma_break = pd.to_numeric(df["ma_breakdown_count"], errors="coerce")
        c5 = pd.to_numeric(df["change_5d"], errors="coerce")
        c0 = pd.to_numeric(df["change_pct"], errors="coerce")

        strong_mask = (
            ma_break.notna() & (ma_break >= ma_break_min)
            & c5.notna() & (c5 <= decline_5d_threshold)
            & c0.notna() & (c0 <= 0)
        )
        # 强信号单独按 strong_penalty 累加；与弱信号叠加后受 cap 限制。
        # 避免一条票因为"恰好两项弱信号 + 一项强信号"就触发 cap，所以用 max(弱累加, 强信号)。
        weak_score = score.copy()
        score = pd.concat(
            [weak_score, strong_mask.astype(float) * strong_penalty], axis=1
        ).max(axis=1)

    return score.clip(lower=0, upper=penalty_cap)


def _compute_capital_heat_quality_score(
    df: pd.DataFrame, profile: dict[str, float]
) -> pd.Series:
    """Score-card adjustment for capital_heat strategy using moneyflow data.

    Capital heat targets stocks with active capital flow — the thesis
    depends on genuine main-force participation, not just high volume.
    This scorer confirms / refutes the thesis with Tushare moneyflow_dc:

    * Positive 5d net inflow → capital_confirmed bonus
    * Strong inflow (% of turnover) → extra bonus
    * Consecutive inflow days → sustained-interest bonus
    * Net outflow → penalty (contradicts the capital-heat thesis)

    When moneyflow data is unavailable the function returns a neutral
    base score (50), so it does not distort results.
    """
    base = float(profile.get("capital_heat_quality_base", 50.0))
    score = pd.Series(base, index=df.index)

    if "mf_available" not in df.columns:
        return score

    mf_avail = df["mf_available"].fillna(False).astype(bool)
    bonus = float(profile.get("capital_heat_capital_confirmed_bonus", 2.4))

    # --- 5-day net inflow: positive = reward, negative = penalise ---
    inflow_min = float(profile.get("capital_heat_mf_inflow_5d_min", 500.0))
    inflow_max = float(profile.get("capital_heat_mf_inflow_5d_max", 5000.0))
    outflow_min = float(profile.get("capital_heat_mf_outflow_5d_min", -500.0))
    outflow_max = float(profile.get("capital_heat_mf_outflow_5d_max", -5000.0))

    if "mf_net_inflow_5d" in df.columns:
        inflow5d = pd.to_numeric(
            df["mf_net_inflow_5d"], errors="coerce"
        ).fillna(0.0)

        # reward positive inflow (up to bonus * 1.5 pts)
        reward_mask = mf_avail & (inflow5d > inflow_min)
        reward_ratio = (
            (inflow5d - inflow_min) / max(inflow_max - inflow_min, 1.0)
        ).clip(lower=0, upper=1.0)
        score = score + reward_mask.astype(float) * reward_ratio * bonus * 1.5

        # penalise outflow (up to -bonus * 0.8 pts)
        outflow_mask = mf_avail & (inflow5d < 0)
        outflow_range = outflow_min - outflow_max
        outflow_ratio = (
            (outflow_min - inflow5d) / max(outflow_range, 1.0)
        ).clip(lower=0, upper=1.0)
        score = score - outflow_mask.astype(float) * outflow_ratio * bonus * 0.8

    # --- Inflow strength (% of turnover): higher = stronger conviction ---
    strength_min = float(profile.get("capital_heat_mf_strength_pct_min", 3.0))
    strength_max = float(profile.get("capital_heat_mf_strength_pct_max", 10.0))
    if "mf_inflow_strength_pct" in df.columns:
        strength = pd.to_numeric(
            df["mf_inflow_strength_pct"], errors="coerce"
        ).fillna(0.0)
        # reward strong inflow %
        strength_reward = mf_avail & (strength > strength_min)
        strength_ratio = (
            (strength - strength_min) / max(strength_max - strength_min, 1.0)
        ).clip(lower=0, upper=1.0)
        score = score + strength_reward.astype(float) * strength_ratio * bonus
        # penalise negative strength
        neg_strength = mf_avail & (strength < 0)
        score = score - neg_strength.astype(float) * (strength / -10.0).clip(
            lower=0, upper=1.0
        ) * bonus * 0.6

    # --- Consecutive days: sustained inflow = genuine interest ---
    if "mf_consecutive_days" in df.columns:
        cons = (
            pd.to_numeric(df["mf_consecutive_days"], errors="coerce")
            .fillna(0)
            .clip(lower=0)
        )
        score = score + (mf_avail & (cons >= 2)).astype(float) * bonus * 0.5
        score = score + (mf_avail & (cons >= 4)).astype(float) * bonus * 0.3

    return score.clip(lower=0, upper=100.0)


def _compute_sector_limitup_ladder_score(
    df: pd.DataFrame, profile: dict[str, float]
) -> pd.Series:
    """连板梯队 + 板块资金活跃加分。

    信号来自两个维度（均为全市场口径，见 industry._attach_industry_limitup_count）：
    1. **同板块涨停家数**（``industry_limitup_count``）：同板块涨停家数越多、
       形成连板梯队，越强；
    2. **板块资金活跃**（``industry_change_pct``）：板块涨幅靠前说明资金在积极做多。

    加分规则：
    - 同板块涨停 ≥2 家 → +5（开始形成梯队）
    - ≥4 家 → 再 +8（梯队明显）
    - ≥6 家 → 再 +8（强连板梯队）
    - 且板块涨幅 >1%（资金活跃）→ 额外 +6

    数据缺失时返回中性 50 分，不扭曲结果。
    """
    base = float(profile.get("sector_limitup_ladder_base", 50.0))
    score = pd.Series(base, index=df.index)

    if "industry_limitup_count" not in df.columns:
        return score

    limitup = (
        pd.to_numeric(df["industry_limitup_count"], errors="coerce")
        .fillna(0)
        .clip(lower=0)
    )
    bonus_2 = float(profile.get("sector_limitup_ladder_2_limitup_bonus", 5.0))
    bonus_4 = float(profile.get("sector_limitup_ladder_4_limitup_bonus", 8.0))
    bonus_6 = float(profile.get("sector_limitup_ladder_6_limitup_bonus", 8.0))

    score = score + (limitup >= 2).astype(float) * bonus_2
    score = score + (limitup >= 4).astype(float) * bonus_4
    score = score + (limitup >= 6).astype(float) * bonus_6

    # 板块资金活跃（涨幅为正且超过阈值）时，在有梯队基础上额外加分
    active_bonus = float(profile.get("sector_limitup_ladder_active_capital_bonus", 6.0))
    active_pct = float(profile.get("sector_limitup_ladder_active_capital_pct", 1.0))
    if "industry_change_pct" in df.columns:
        ind_chg = pd.to_numeric(df["industry_change_pct"], errors="coerce").fillna(0)
        active = (limitup >= 2) & (ind_chg > active_pct)
        score = score + active.astype(float) * active_bonus

    return score.clip(lower=0, upper=100.0)
