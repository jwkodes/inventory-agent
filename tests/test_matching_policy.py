"""Tests for explainable item-match confidence decisions."""

from decimal import Decimal
from uuid import UUID

from inventory_agent.matching.models import (
    CandidateMatchMethod,
    InventoryCandidate,
    MatchDecisionStatus,
)
from inventory_agent.matching.policy import MatchConfidencePolicy


def candidate(
    *,
    variant_suffix: int,
    method: CandidateMatchMethod,
    score: str,
) -> InventoryCandidate:
    return InventoryCandidate(
        item_variant_id=UUID(f"21000000-0000-0000-0000-{variant_suffix:012d}"),
        item_id=UUID(f"20000000-0000-0000-0000-{variant_suffix:012d}"),
        item_name=f"Item {variant_suffix}",
        variant_name=None,
        sku=f"SKU-{variant_suffix}",
        base_unit="each",
        tracking_mode="simple",
        match_method=method,
        match_score=Decimal(score),
        match_evidence={"source": "test"},
    )


def test_unique_exact_identifier_is_selected() -> None:
    exact = candidate(
        variant_suffix=1,
        method=CandidateMatchMethod.EXACT_IDENTIFIER,
        score="1.0",
    )

    decision = MatchConfidencePolicy().decide([exact])

    assert decision.status is MatchDecisionStatus.MATCHED
    assert decision.selected == exact


def test_colliding_trusted_matches_require_confirmation() -> None:
    candidates = [
        candidate(
            variant_suffix=1,
            method=CandidateMatchMethod.EXACT_IDENTIFIER,
            score="1.0",
        ),
        candidate(
            variant_suffix=2,
            method=CandidateMatchMethod.CONFIRMED_ALIAS,
            score="0.99",
        ),
    ]

    decision = MatchConfidencePolicy().decide(candidates)

    assert decision.status is MatchDecisionStatus.NEEDS_CONFIRMATION
    assert decision.selected is None


def test_strong_well_separated_fuzzy_match_is_selected() -> None:
    candidates = [
        candidate(
            variant_suffix=1,
            method=CandidateMatchMethod.TEXT_SEARCH,
            score="0.82",
        ),
        candidate(
            variant_suffix=2,
            method=CandidateMatchMethod.TEXT_SEARCH,
            score="0.60",
        ),
    ]

    decision = MatchConfidencePolicy().decide(candidates)

    assert decision.status is MatchDecisionStatus.MATCHED
    assert decision.selected == candidates[0]


def test_close_fuzzy_candidates_require_confirmation() -> None:
    candidates = [
        candidate(
            variant_suffix=1,
            method=CandidateMatchMethod.TEXT_SEARCH,
            score="0.80",
        ),
        candidate(
            variant_suffix=2,
            method=CandidateMatchMethod.TEXT_SEARCH,
            score="0.75",
        ),
    ]

    decision = MatchConfidencePolicy().decide(candidates)

    assert decision.status is MatchDecisionStatus.NEEDS_CONFIRMATION


def test_clear_semantic_candidate_is_selected_on_its_own_scale() -> None:
    candidates = [
        candidate(
            variant_suffix=1,
            method=CandidateMatchMethod.SEMANTIC_RERANK,
            score="0.45",
        ),
        candidate(
            variant_suffix=2,
            method=CandidateMatchMethod.SEMANTIC_RERANK,
            score="0.25",
        ),
    ]

    decision = MatchConfidencePolicy().decide(candidates)

    assert decision.status is MatchDecisionStatus.MATCHED
    assert decision.selected == candidates[0]


def test_ambiguous_semantic_candidates_require_confirmation() -> None:
    candidates = [
        candidate(
            variant_suffix=1,
            method=CandidateMatchMethod.SEMANTIC_RERANK,
            score="0.42",
        ),
        candidate(
            variant_suffix=2,
            method=CandidateMatchMethod.SEMANTIC_RERANK,
            score="0.38",
        ),
    ]

    decision = MatchConfidencePolicy().decide(candidates)

    assert decision.status is MatchDecisionStatus.NEEDS_CONFIRMATION


def test_no_candidates_returns_not_found() -> None:
    decision = MatchConfidencePolicy().decide([])

    assert decision.status is MatchDecisionStatus.NOT_FOUND
    assert decision.selected is None
