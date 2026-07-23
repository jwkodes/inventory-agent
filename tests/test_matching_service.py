"""Tests for matching extracted lines through the repository and policy."""

from decimal import Decimal
from uuid import UUID

from inventory_agent.extraction.schema import ExtractedCommandLine, ItemReferenceType
from inventory_agent.matching.judge import CandidateJudgeOutput
from inventory_agent.matching.models import (
    CandidateMatchMethod,
    InventoryCandidate,
    MatchDecisionStatus,
)
from inventory_agent.matching.service import (
    InventoryItemMatcher,
    MatchingStrategy,
)

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")


class RecordingRepository:
    def __init__(
        self,
        candidates: list[InventoryCandidate],
        fallback_candidates: list[InventoryCandidate] | None = None,
    ) -> None:
        self.candidates = candidates
        self.fallback_candidates = fallback_candidates or []
        self.query: str | None = None
        self.reference_type: ItemReferenceType | None = None

    async def find_candidates(
        self,
        *,
        organization_id: UUID,
        query: str,
        reference_type: ItemReferenceType,
        supplier_scope: str | None = None,
        limit: int = 5,
    ) -> list[InventoryCandidate]:
        assert organization_id == ORGANIZATION_ID
        self.query = query
        self.reference_type = reference_type
        return self.candidates

    async def browse_candidates(
        self,
        *,
        organization_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[InventoryCandidate]:
        assert organization_id == ORGANIZATION_ID
        assert query == self.query
        return self.fallback_candidates


class RecordingSemanticRepository:
    def __init__(self, candidates: list[InventoryCandidate]) -> None:
        self.candidates = candidates
        self.queries: list[str] = []

    async def find_candidates(
        self,
        *,
        organization_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[InventoryCandidate]:
        assert organization_id == ORGANIZATION_ID
        self.queries.append(query)
        return self.candidates


class FixedJudge:
    def __init__(self, output: CandidateJudgeOutput) -> None:
        self.output = output
        self.lines: list[ExtractedCommandLine] = []

    async def judge(
        self,
        *,
        line: ExtractedCommandLine,
        candidates: list[InventoryCandidate],
        clarification_replies: list[str] | None = None,
        accumulated_attributes: dict[str, str] | None = None,
    ) -> CandidateJudgeOutput:
        self.lines.append(line)
        return self.output


def candidate(
    *,
    suffix: int,
    method: CandidateMatchMethod,
    score: str,
) -> InventoryCandidate:
    return InventoryCandidate(
        item_variant_id=UUID(f"21000000-0000-0000-0000-{suffix:012d}"),
        item_id=UUID(f"20000000-0000-0000-0000-{suffix:012d}"),
        item_name=f"Controller {suffix}",
        variant_name=None,
        sku=f"CTRL-{suffix}",
        base_unit="each",
        tracking_mode="simple",
        match_method=method,
        match_score=Decimal(score),
        match_evidence={"source": "test"},
    )


def controller_line() -> ExtractedCommandLine:
    return ExtractedCommandLine(
        source_text="4 switch2 controller",
        item_reference={"type": "UNKNOWN", "value": "switch2 controller"},
        description="switch2 controller",
        quantity="4",
        unit=None,
        attributes=[],
    )


async def test_matcher_uses_explicit_item_reference_before_description() -> None:
    repository = RecordingRepository([])
    line = ExtractedCommandLine(
        source_text="three boxes of medicine",
        item_reference={"type": "PART_NUMBER", "value": "AMOX-500"},
        description="amoxicillin",
        quantity="3",
        unit="box",
        attributes=[],
    )

    decision = await InventoryItemMatcher(repository=repository).match_line(
        organization_id=ORGANIZATION_ID,
        line=line,
    )

    assert repository.query == "AMOX-500"
    assert repository.reference_type is ItemReferenceType.PART_NUMBER
    assert decision.status is MatchDecisionStatus.NOT_FOUND


async def test_semantic_strategy_is_used_for_name_matching() -> None:
    semantic_match = candidate(
        suffix=1,
        method=CandidateMatchMethod.SEMANTIC_RERANK,
        score="0.91",
    )
    semantic = RecordingSemanticRepository([semantic_match])

    decision = await InventoryItemMatcher(
        repository=RecordingRepository([]),
        semantic_repository=semantic,
        strategy=MatchingStrategy.SEMANTIC,
    ).match_line(
        organization_id=ORGANIZATION_ID,
        line=controller_line(),
    )

    assert semantic.queries == ["switch2 controller"]
    assert decision.status is MatchDecisionStatus.MATCHED
    assert decision.selected == semantic_match


async def test_exact_identifier_bypasses_semantic_matching() -> None:
    exact = candidate(
        suffix=1,
        method=CandidateMatchMethod.EXACT_IDENTIFIER,
        score="1",
    )
    semantic = RecordingSemanticRepository(
        [
            candidate(
                suffix=2,
                method=CandidateMatchMethod.SEMANTIC_RERANK,
                score="0.99",
            )
        ]
    )

    decision = await InventoryItemMatcher(
        repository=RecordingRepository([exact]),
        semantic_repository=semantic,
        strategy=MatchingStrategy.SEMANTIC,
    ).match_line(
        organization_id=ORGANIZATION_ID,
        line=controller_line(),
    )

    assert semantic.queries == []
    assert decision.selected == exact


async def test_fuzzy_strategy_remains_available_without_embeddings() -> None:
    fuzzy = candidate(
        suffix=1,
        method=CandidateMatchMethod.TEXT_SEARCH,
        score="0.85",
    )

    decision = await InventoryItemMatcher(
        repository=RecordingRepository([fuzzy]),
        strategy=MatchingStrategy.FUZZY,
    ).match_line(
        organization_id=ORGANIZATION_ID,
        line=controller_line(),
    )

    assert decision.status is MatchDecisionStatus.MATCHED
    assert decision.selected == fuzzy


async def test_candidate_judge_rejects_semantically_similar_wrong_edition() -> None:
    switch_two = candidate(
        suffix=2,
        method=CandidateMatchMethod.SEMANTIC_RERANK,
        score="0.91",
    )
    line = ExtractedCommandLine(
        source_text="6 first edition nintendo switch contrler",
        item_reference={"type": "NAME", "value": "nintendo switch controller"},
        description="nintendo switch controller",
        quantity="6",
        unit=None,
        attributes=[{"key": "edition", "value": "first edition"}],
    )
    judge = FixedJudge(
        CandidateJudgeOutput(
            action="NO_MATCH",
            selected_candidate_id=None,
            question=None,
            reason="The offered controller is Switch 2, not first edition.",
        )
    )
    semantic = RecordingSemanticRepository([switch_two])

    decision = await InventoryItemMatcher(
        repository=RecordingRepository([]),
        semantic_repository=semantic,
        judge=judge,
        strategy=MatchingStrategy.SEMANTIC,
    ).match_line(organization_id=ORGANIZATION_ID, line=line)

    assert semantic.queries == ["nintendo switch controller | edition: first edition"]
    assert decision.status is MatchDecisionStatus.NOT_FOUND
    assert decision.selected is None
    assert "not first edition" in decision.reason


async def test_candidate_judge_can_request_a_colour_clarification() -> None:
    red = candidate(
        suffix=1,
        method=CandidateMatchMethod.SEMANTIC_RERANK,
        score="0.88",
    )
    blue = candidate(
        suffix=2,
        method=CandidateMatchMethod.SEMANTIC_RERANK,
        score="0.86",
    )
    judge = FixedJudge(
        CandidateJudgeOutput(
            action="ASK_USER",
            selected_candidate_id=None,
            question="Which colour is it?",
            reason="Red and blue variants are both plausible.",
        )
    )

    decision = await InventoryItemMatcher(
        repository=RecordingRepository([]),
        semantic_repository=RecordingSemanticRepository([red, blue]),
        judge=judge,
        strategy=MatchingStrategy.SEMANTIC,
    ).match_line(organization_id=ORGANIZATION_ID, line=controller_line())

    assert decision.status is MatchDecisionStatus.CLARIFICATION_REQUIRED
    assert decision.clarification_question == "Which colour is it?"
    assert decision.candidates == [red, blue]
