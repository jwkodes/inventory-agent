"""Tests for matching extracted lines through the repository and policy."""

from uuid import UUID

from inventory_agent.extraction.schema import ExtractedCommandLine, ItemReferenceType
from inventory_agent.matching.models import InventoryCandidate, MatchDecisionStatus
from inventory_agent.matching.service import InventoryItemMatcher

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")


class RecordingRepository:
    def __init__(self, candidates: list[InventoryCandidate]) -> None:
        self.candidates = candidates
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
