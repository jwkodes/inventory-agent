"""Orchestrate candidate retrieval and confidence policy per extracted line."""

from uuid import UUID

from inventory_agent.extraction.schema import ExtractedCommandLine
from inventory_agent.matching.models import MatchDecision
from inventory_agent.matching.policy import MatchConfidencePolicy
from inventory_agent.matching.repository import InventoryCandidateRepository


class InventoryItemMatcher:
    def __init__(
        self,
        *,
        repository: InventoryCandidateRepository,
        policy: MatchConfidencePolicy | None = None,
    ) -> None:
        self._repository = repository
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
        candidates = await self._repository.find_candidates(
            organization_id=organization_id,
            query=query,
            reference_type=line.item_reference.type,
            supplier_scope=supplier_scope,
            limit=limit,
        )
        return self._policy.decide(candidates)
