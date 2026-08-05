"""Regression tests for the consolidation_breakout quality factor.

These tests use hand-constructed feature rows that mirror the 2026-08-04
screening candidates for 东富龙 / 东贝集团 / 德生科技. They verify that the
`consolidation_quality` factor is actually produced by the scorer and that a
long-base, gentle-volume breakout candidate (德生科技) scores higher than a
short-base, erratic-volume candidate.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.services.screening.models import ScreeningConfig
from src.services.screening.scorer import (
    _compute_consolidation_quality_score,
    compute_screen_scores,
)


@pytest.fixture
def consolidation_candidates() -> pd.DataFrame:
    """Three candidates ranked 1/2/3 by the legacy screen_score on 2026-08-04."""
    return pd.DataFrame(
        [
            {
                "code": "300171",
                "name": "东富龙",
                "amount": 2.5e8,
                "pe_ratio": 35.0,
                "pb_ratio": 3.2,
                "total_mv": 8.0e9,
                "turnover_rate": 2.5,
                "volume_ratio": 1.5,
                "change_pct": 2.86,
                "change_60d": 12.0,
                "range_20d_pct": 17.03,
                "volatility_20d_pct": 42.72,
                "max_drawdown_20d_pct": -8.0,
                "atr_20_pct": 2.5,
                "consolidation_days_20d": 16,
                "consolidation_days_60d": 0,
                "consolidation_days_120d": 0,
                "breakout_20d_pct": -2.0,
                "change_10d": 5.0,
                "change_20d": 7.0,
                "volume_expand_1d": 1.3,
                "volume_expand_5d": 1.1,
                "consecutive_volume_spike_2d": False,
                "consecutive_volume_spike_3d": False,
                "coiled_spring_contraction_pct": 5.0,
                "coiled_spring_ramp_ratio": 1.05,
                "ma_bullish": True,
                "price_above_ma20": True,
                "macd_status": "bullish",
                "signal_score": 55.0,
                "board_heat_score": 60.0,
                "daily_quality_score": 85.0,
                "daily_quality_flags": "",
                "industry": "医疗器械",
                "concepts": "医疗器械|创新药",
            },
            {
                "code": "601956",
                "name": "东贝集团",
                "amount": 1.8e8,
                "pe_ratio": 22.0,
                "pb_ratio": 2.1,
                "total_mv": 4.0e9,
                "turnover_rate": 1.8,
                "volume_ratio": 1.2,
                "change_pct": 1.84,
                "change_60d": 8.0,
                "range_20d_pct": 18.94,
                "volatility_20d_pct": 34.40,
                "max_drawdown_20d_pct": -6.0,
                "atr_20_pct": 2.0,
                "consolidation_days_20d": 7,
                "consolidation_days_60d": 0,
                "consolidation_days_120d": 0,
                "breakout_20d_pct": 0.81,
                "change_10d": 11.71,
                "change_20d": 3.55,
                "volume_expand_1d": 2.42,
                "volume_expand_5d": 1.42,
                "consecutive_volume_spike_2d": True,
                "consecutive_volume_spike_3d": True,
                "coiled_spring_contraction_pct": 8.0,
                "coiled_spring_ramp_ratio": 1.1,
                "ma_bullish": True,
                "price_above_ma20": True,
                "macd_status": "bullish",
                "signal_score": 52.0,
                "board_heat_score": 50.0,
                "daily_quality_score": 88.0,
                "daily_quality_flags": "",
                "industry": "家电零部件II",
                "concepts": "家电|新能源车",
            },
            {
                "code": "002908",
                "name": "德生科技",
                "amount": 3.5e7,
                "pe_ratio": 55.0,
                "pb_ratio": 4.5,
                "total_mv": 2.0e9,
                "turnover_rate": 3.2,
                "volume_ratio": 1.1,
                "change_pct": 1.76,
                "change_60d": 10.0,
                "range_20d_pct": 15.25,
                "volatility_20d_pct": 34.21,
                "max_drawdown_20d_pct": -5.0,
                "atr_20_pct": 1.5,
                "consolidation_days_20d": 14,
                "consolidation_days_60d": 20,
                "consolidation_days_120d": 0,
                "breakout_20d_pct": 0.73,
                "change_10d": 7.66,
                "change_20d": 8.16,
                "volume_expand_1d": 1.10,
                "volume_expand_5d": 1.42,
                "consecutive_volume_spike_2d": True,
                "consecutive_volume_spike_3d": True,
                "coiled_spring_contraction_pct": -42.11,
                "coiled_spring_ramp_ratio": 1.25,
                "ma_bullish": True,
                "price_above_ma20": True,
                "macd_status": "bullish",
                "signal_score": 58.0,
                "board_heat_score": 45.0,
                "daily_quality_score": 90.0,
                "daily_quality_flags": "",
                "industry": "IT服务II",
                "concepts": "数字货币|智慧政务|数据要素",
            },
        ]
    )


@pytest.fixture
def consolidation_config() -> ScreeningConfig:
    return ScreeningConfig(
        enabled=True,
        factor_weights={
            "activity": 0.12,
            "consolidation_quality": 0.62,
            "liquidity": 0.0,
            "momentum": 0.10,
            "stability": 0.08,
            "theme_heat": 0.08,
            "value": 0.0,
        },
    )


def test_consolidation_quality_factor_is_present_in_scores(
    consolidation_candidates: pd.DataFrame,
    consolidation_config: ScreeningConfig,
) -> None:
    result = compute_screen_scores(consolidation_candidates, consolidation_config)
    assert "factor_consolidation_quality_score" in result.columns
    assert result["factor_consolidation_quality_score"].notna().all()


def test_long_base_candidate_outranks_short_base_candidate(
    consolidation_candidates: pd.DataFrame,
    consolidation_config: ScreeningConfig,
) -> None:
    """A 14-day tight base with controlled volume should beat a 7-day erratic one."""
    result = compute_screen_scores(consolidation_candidates, consolidation_config)
    ranked = result.sort_values("screen_score", ascending=False).reset_index(drop=True)

    cq_scores = dict(
        zip(result["code"], result["factor_consolidation_quality_score"])
    )
    assert cq_scores["002908"] > cq_scores["601956"]
    assert ranked.iloc[0]["code"] == "002908"


def test_consolidation_quality_score_components(
    consolidation_candidates: pd.DataFrame,
) -> None:
    """Direct unit check of the quality scoring function."""
    from src.services.screening.scorer import _scoring_profile

    profile = _scoring_profile(
        ScreeningConfig(enabled=True, scoring_profile={})
    )
    scores = _compute_consolidation_quality_score(
        consolidation_candidates, profile
    )
    by_code = dict(zip(consolidation_candidates["code"], scores))
    assert by_code["002908"] > by_code["300171"]
    assert by_code["002908"] > by_code["601956"]
