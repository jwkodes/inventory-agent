"""Calibratable confidence policy kept separate from candidate retrieval."""

from decimal import Decimal

from inventory_agent.matching.models import (
    CandidateMatchMethod,
    InventoryCandidate,
    MatchDecision,
    MatchDecisionStatus,
)


class MatchConfidencePolicy:
    """Decide whether a ranked result is safe or needs human selection."""

    def __init__(
        self,
        *,
        fuzzy_score_threshold: Decimal = Decimal("0.72"),
        fuzzy_margin_threshold: Decimal = Decimal("0.12"),
        trusted_collision_margin: Decimal = Decimal("0.02"),
    ) -> None:
        self._fuzzy_score_threshold = fuzzy_score_threshold
        self._fuzzy_margin_threshold = fuzzy_margin_threshold
        self._trusted_collision_margin = trusted_collision_margin

    def decide(self, candidates: list[InventoryCandidate]) -> MatchDecision:
        """Use method strength, absolute score, and top-two margin."""

        ranked = sorted(candidates, key=lambda candidate: candidate.match_score, reverse=True)
        if not ranked:
            return MatchDecision(
                status=MatchDecisionStatus.NOT_FOUND,
                selected=None,
                candidates=[],
                reason="No catalog candidate met the retrieval threshold",
            )

        top = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        margin = top.match_score - runner_up.match_score if runner_up else top.match_score

        trusted_methods = {
            CandidateMatchMethod.EXACT_IDENTIFIER,
            CandidateMatchMethod.CONFIRMED_ALIAS,
        }
        if top.match_method in trusted_methods:
            if (
                runner_up is not None
                and runner_up.match_method in trusted_methods
                and margin < self._trusted_collision_margin
            ):
                return MatchDecision(
                    status=MatchDecisionStatus.NEEDS_CONFIRMATION,
                    selected=None,
                    candidates=ranked,
                    reason="Multiple trusted identifiers or aliases point to different variants",
                )
            return MatchDecision(
                status=MatchDecisionStatus.MATCHED,
                selected=top,
                candidates=ranked,
                reason=f"Resolved by {top.match_method.value}",
            )

        if (
            top.match_score >= self._fuzzy_score_threshold
            and margin >= self._fuzzy_margin_threshold
        ):
            return MatchDecision(
                status=MatchDecisionStatus.MATCHED,
                selected=top,
                candidates=ranked,
                reason="Fuzzy candidate exceeded the score and separation thresholds",
            )

        return MatchDecision(
            status=MatchDecisionStatus.NEEDS_CONFIRMATION,
            selected=None,
            candidates=ranked,
            reason="Fuzzy evidence is weak or ambiguous",
        )
