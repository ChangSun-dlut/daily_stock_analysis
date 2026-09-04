# -*- coding: utf-8 -*-
"""Tests for batch (map-reduce) LLM ranking in screening/ranker.py."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from src.services.screening.models import Pick
from src.services.screening.ranker import rank_candidates_with_metadata


def _make_pick(index: int, screen_score: float | None = None) -> Pick:
    return Pick(
        rank=index + 1,
        code=f"{index:04d}",
        name=f"name-{index}",
        final_score=screen_score if screen_score is not None else (100.0 - index),
        screen_score=screen_score if screen_score is not None else (100.0 - index),
    )


def _ranking_response(*codes: str, extra_fields: dict[str, object] | None = None) -> str:
    """Build a full ranking JSON response."""
    ranked = [
        {
            "code": code,
            "llm_score": 90 - idx,
            "confidence": 0.8,
            "reason": f"reason-{code}",
            "risk": "risk",
        }
        for idx, code in enumerate(codes)
    ]
    payload: dict[str, object] = {
        "market_view": "batch view",
        "selection_logic": "batch logic",
        "portfolio_risk": "batch risk",
        "ranked": ranked,
    }
    if extra_fields:
        payload.update(extra_fields)
    return json.dumps(payload, ensure_ascii=False)


def test_auto_batch_ranks_in_batches_and_returns_final_metadata():
    """30 candidates (>20 auto threshold) should trigger 2 batches (15+15) + final."""
    candidates = [_make_pick(i) for i in range(30)]
    call_log: list[str] = []

    def fake_call_llm(prompt, *args, **kwargs):
        call_log.append(prompt)
        # Batch 1: codes 0000-0014. Return them reversed (high-index first).
        if "0000" in prompt and "0014" in prompt:
            return _ranking_response(*[f"{i:04d}" for i in range(14, -1, -1)])
        # Batch 2: codes 0015-0029. Return them reversed.
        if "0015" in prompt and "0029" in prompt:
            return _ranking_response(*[f"{i:04d}" for i in range(29, 14, -1)])
        # Final prompt should contain only promoted finalists (top 8 from each batch).
        # batch1 top 8 = 0014..0007; batch2 top 8 = 0029..0022.
        return _ranking_response(
            "0029", "0028", "0014", "0013",
            "0027", "0026", "0012", "0011",
            "0025", "0024", "0010", "0009",
            "0023", "0022", "0008", "0007",
        )

    with patch("src.services.screening.ranker._call_llm", side_effect=fake_call_llm):
        result = rank_candidates_with_metadata(
            candidates,
            ranking_hints="test",
            llm_api_key="key",
            llm_model="model",
            rank_weight=1.0,
        )

    assert result.ranked is True
    assert len(call_log) == 3  # 2 batches + 1 final
    # Batch stage uses rank_weight=1.0, so promotion is LLM-driven:
    # top 8 from batch1 = 0014..0007; top 8 from batch2 = 0029..0022.
    final_prompt = call_log[-1]
    assert "0014" in final_prompt and "0007" in final_prompt
    assert "0029" in final_prompt and "0022" in final_prompt
    # The winner from the mocked final ranking should be first.
    assert result.picks[0].code == "0029"
    assert result.market_view == "batch view"


def test_batch_stage_fallback_still_promotes_by_screen_score():
    """If a batch returns no JSON, that batch still promotes by screen_score."""
    candidates = [_make_pick(i) for i in range(30)]
    call_log: list[str] = []

    def fake_call_llm(prompt, *args, **kwargs):
        call_log.append(prompt)
        # Batch 1 returns valid ranking; batch 2 returns garbage (no JSON).
        if "0000" in prompt and "0014" in prompt:
            return _ranking_response(*[f"{i:04d}" for i in range(14, -1, -1)])
        if "0015" in prompt and "0029" in prompt:
            return "no json here"
        # Final over promoted finalists.
        # batch1 top 8 = 0014..0007; batch2 fallback top 8 by screen_score = 0015..0022.
        return _ranking_response(
            "0014", "0013", "0015", "0016",
            "0012", "0011", "0017", "0018",
            "0010", "0009", "0019", "0020",
            "0008", "0007", "0021", "0022",
        )

    with patch("src.services.screening.ranker._call_llm", side_effect=fake_call_llm):
        result = rank_candidates_with_metadata(
            candidates,
            ranking_hints="test",
            llm_api_key="key",
            llm_model="model",
            rank_weight=1.0,
        )

    assert result.ranked is True
    # Batch 2 fallback promoted by screen_score -> 0015 is the highest in that slice.
    final_prompt = call_log[-1]
    assert "0015" in final_prompt
    assert result.picks[0].code == "0014"


def test_explicit_batch_size_zero_disables_batching():
    """batch_size=0 forces single-shot ranking even with many candidates."""
    candidates = [_make_pick(i) for i in range(30)]
    call_count = 0

    def fake_call_llm(prompt, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        assert "0000" in prompt and "0029" in prompt
        return _ranking_response(*[f"{i:04d}" for i in range(29, -1, -1)])

    with patch("src.services.screening.ranker._call_llm", side_effect=fake_call_llm):
        result = rank_candidates_with_metadata(
            candidates,
            ranking_hints="test",
            llm_api_key="key",
            llm_model="model",
            batch_size=0,
        )

    assert result.ranked is True
    assert call_count == 1


def test_small_pool_does_not_batch_by_default():
    """10 candidates (<20 auto threshold) should use single-shot ranking."""
    candidates = [_make_pick(i) for i in range(10)]
    call_count = 0

    def fake_call_llm(prompt, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _ranking_response(*[f"{i:04d}" for i in range(9, -1, -1)])

    with patch("src.services.screening.ranker._call_llm", side_effect=fake_call_llm):
        result = rank_candidates_with_metadata(
            candidates,
            ranking_hints="test",
            llm_api_key="key",
            llm_model="model",
        )

    assert result.ranked is True
    assert call_count == 1
