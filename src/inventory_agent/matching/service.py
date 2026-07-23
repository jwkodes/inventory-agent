"""Orchestrate candidate retrieval and confidence policy per extracted line."""

from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from inventory_agent.extraction.schema import ExtractedCommandLine
from inventory_agent.matching.judge import CandidateJudge, CandidateJudgeAction
from inventory_agent.matching.models import (
    CandidateMatchMethod,
    InventoryCandidate,
    MatchDecision,
    MatchDecisionStatus,
)
from inventory_agent.matching.policy import MatchConfidencePolicy
from inventory_agent.matching.repository import InventoryCandidateRepository
from inventory_agent.matching.semantic import SemanticCandidateRepository


class MatchingStrategy(StrEnum):
    SEMANTIC = "semantic"
    FUZZY = "fuzzy"
    HYBRID = "hybrid"


class InventoryItemMatcher:
    def __init__(
        self,
        *,
        repository: InventoryCandidateRepository,
        semantic_repository: SemanticCandidateRepository | None = None,
        judge: CandidateJudge | None = None,
        strategy: MatchingStrategy = MatchingStrategy.FUZZY,
        policy: MatchConfidencePolicy | None = None,
    ) -> None:
        self._repository = repository
        self._semantic_repository = semantic_repository
        self._judge = judge
        self._strategy = strategy
        self._policy = policy or MatchConfidencePolicy()

    async def match_line(
        self,
        *,
        organization_id: UUID,
        line: ExtractedCommandLine,
        supplier_scope: str | None = None,
        limit: int = 5,
    ) -> MatchDecision:
        """Retrieve candidates using the most specific source wording available."""

        query = line.item_reference.value or line.description or line.source_text
        semantic_query = _matching_query(line)
        candidates = await self._repository.find_candidates(
            organization_id=organization_id,
            query=query,
            reference_type=line.item_reference.type,
            supplier_scope=supplier_scope,
            limit=limit,
        )
        trusted = [
            candidate
            for candidate in candidates
            if candidate.match_method
            in {
                CandidateMatchMethod.EXACT_IDENTIFIER,
                CandidateMatchMethod.CONFIRMED_ALIAS,
            }
        ]
        if trusted and (self._judge is None or not line.attributes):
            return self._policy.decide(trusted)

        ranked = trusted or candidates
        if not trusted and self._strategy is not MatchingStrategy.FUZZY:
            if self._semantic_repository is None:
                raise RuntimeError("Semantic matching is configured without a repository")
            semantic = await self._semantic_repository.find_candidates(
                organization_id=organization_id,
                query=semantic_query,
                limit=limit,
            )
            ranked = (
                semantic
                if self._strategy is MatchingStrategy.SEMANTIC
                else _hybrid_candidates(semantic=semantic, fuzzy=candidates)
            )

        if self._judge is not None and ranked:
            judgment = await self._judge.judge(line=line, candidates=ranked)
            if judgment.action is CandidateJudgeAction.ASK_USER:
                return MatchDecision(
                    status=MatchDecisionStatus.CLARIFICATION_REQUIRED,
                    selected=None,
                    candidates=ranked,
                    reason=judgment.reason,
                    clarification_question=judgment.question,
                )
            if judgment.action is CandidateJudgeAction.NO_MATCH:
                return await self._not_found_with_fallback(
                    organization_id=organization_id,
                    query=query,
                    candidates=ranked,
                    reason=judgment.reason,
                    limit=limit,
                )

            selected_id = judgment.selected_candidate_id
            selected = next(
                (candidate for candidate in ranked if candidate.item_variant_id == selected_id),
                None,
            )
            policy_decision = self._policy.decide(ranked)
            if (
                selected is not None
                and policy_decision.status is MatchDecisionStatus.MATCHED
                and policy_decision.selected is not None
                and policy_decision.selected.item_variant_id == selected.item_variant_id
            ):
                return MatchDecision(
                    status=MatchDecisionStatus.MATCHED,
                    selected=selected,
                    candidates=ranked,
                    reason=judgment.reason,
                )
            return MatchDecision(
                status=MatchDecisionStatus.NEEDS_CONFIRMATION,
                selected=None,
                candidates=ranked,
                reason=(f"{judgment.reason} Retrieval confidence still requires confirmation."),
            )

        decision = self._policy.decide(ranked)
        if decision.status is MatchDecisionStatus.NOT_FOUND:
            return await self._not_found_with_fallback(
                organization_id=organization_id,
                query=query,
                candidates=ranked,
                reason="No catalog candidate met the normal retrieval threshold",
                limit=limit,
            )
        return decision

    async def _not_found_with_fallback(
        self,
        *,
        organization_id: UUID,
        query: str,
        candidates: list[InventoryCandidate],
        reason: str,
        limit: int,
    ) -> MatchDecision:
        fallback = await self._repository.browse_candidates(
            organization_id=organization_id,
            query=query,
            limit=limit,
        )
        return MatchDecision(
            status=MatchDecisionStatus.NOT_FOUND,
            selected=None,
            candidates=fallback or candidates,
            reason=reason,
        )


def _matching_query(line: ExtractedCommandLine) -> str:
    """Include explicit qualifiers in retrieval while keeping quantity out."""

    subject = line.item_reference.value or line.description or line.source_text
    qualifiers = " | ".join(f"{attribute.key}: {attribute.value}" for attribute in line.attributes)
    return f"{subject} | {qualifiers}" if qualifiers else subject


def _hybrid_candidates(
    *,
    semantic: list[InventoryCandidate],
    fuzzy: list[InventoryCandidate],
) -> list[InventoryCandidate]:
    semantic_by_id = {candidate.item_variant_id: candidate for candidate in semantic}
    fuzzy_by_id = {candidate.item_variant_id: candidate for candidate in fuzzy}
    combined: list[InventoryCandidate] = []
    for variant_id in semantic_by_id.keys() | fuzzy_by_id.keys():
        semantic_candidate = semantic_by_id.get(variant_id)
        fuzzy_candidate = fuzzy_by_id.get(variant_id)
        source = semantic_candidate or fuzzy_candidate
        if source is None:
            continue
        if semantic_candidate is not None and fuzzy_candidate is not None:
            score = semantic_candidate.match_score * Decimal(
                "0.7"
            ) + fuzzy_candidate.match_score * Decimal("0.3")
        else:
            score = source.match_score
        combined.append(
            source.model_copy(
                update={
                    "match_method": CandidateMatchMethod.SEMANTIC_RERANK,
                    "match_score": score,
                    "match_evidence": {
                        "source": "hybrid",
                        "semantic_score": (
                            str(semantic_candidate.match_score)
                            if semantic_candidate is not None
                            else None
                        ),
                        "fuzzy_score": (
                            str(fuzzy_candidate.match_score)
                            if fuzzy_candidate is not None
                            else None
                        ),
                    },
                }
            )
        )
    return sorted(combined, key=lambda candidate: candidate.match_score, reverse=True)
