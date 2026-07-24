"""Grounding and proposal tests for production Telegram agent tools."""

import json
from decimal import Decimal
from uuid import UUID

from inventory_agent.agent.models import (
    CatalogVariant,
    InventoryReadArguments,
    TransactionRecord,
)
from inventory_agent.agent.production_tools import (
    GroundedAgentCatalogReader,
    ProductionInventoryAgentTools,
    ProductionToolContext,
)
from inventory_agent.agent.repository import AgentReadRepository
from inventory_agent.extraction.schema import ItemReferenceType
from inventory_agent.matching.models import InventoryCandidate
from inventory_agent.matching.service import MatchingStrategy
from inventory_agent.proposals.models import ProposalDraft

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
LOCATION_ID = UUID("12000000-0000-0000-0000-000000000001")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000001")
REVERSAL_REQUEST_ID = UUID("70000000-0000-0000-0000-000000000001")


class FakeCatalog:
    async def read(
        self,
        *,
        organization_id: UUID,
        location_id: UUID,
        arguments: InventoryReadArguments,
    ) -> tuple[list[CatalogVariant], dict[UUID, InventoryCandidate]]:
        candidate = InventoryCandidate(
            item_variant_id=VARIANT_ID,
            item_id=UUID("20000000-0000-0000-0000-000000000001"),
            item_name="Industrial Widget",
            variant_name="Standard",
            sku="ABC-123",
            base_unit="each",
            tracking_mode="simple",
            match_method="exact_identifier",
            match_score=Decimal("1"),
            match_evidence={},
        )
        return (
            [
                CatalogVariant(
                    variant_id=str(VARIANT_ID),
                    item_name="Industrial Widget",
                    variant_name="Standard",
                    sku="ABC-123",
                    on_hand=Decimal("10"),
                )
            ],
            {VARIANT_ID: candidate},
        )


class AliasCandidateRepository:
    async def find_candidates(
        self,
        *,
        organization_id: UUID,
        query: str,
        reference_type: ItemReferenceType,
        supplier_scope: str | None = None,
        limit: int = 5,
    ) -> list[InventoryCandidate]:
        assert query == "AMOX-500"
        assert reference_type is ItemReferenceType.SKU
        return [
            InventoryCandidate(
                item_variant_id=VARIANT_ID,
                item_id=UUID("20000000-0000-0000-0000-000000000001"),
                item_name="Amoxicillin 500 mg",
                variant_name=None,
                sku="MED-AMOX-500",
                base_unit="box",
                tracking_mode="lot",
                match_method="exact_identifier",
                match_score=Decimal("1"),
                match_evidence={"matched_value": "AMOX-500"},
            )
        ]

    async def browse_candidates(
        self,
        *,
        organization_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[InventoryCandidate]:
        raise AssertionError("not expected")


class FakeReads(AgentReadRepository):
    async def get_variant_balances(
        self,
        *,
        organization_id: UUID,
        location_id: UUID,
        variant_ids: list[UUID],
    ) -> dict[UUID, Decimal]:
        return {VARIANT_ID: Decimal("10")}

    async def read_transactions(
        self,
        *,
        organization_id: UUID,
        query: str | None,
        limit: int,
    ) -> list[TransactionRecord]:
        return []


class FallbackTransactionReads(FakeReads):
    def __init__(self) -> None:
        self.queries: list[str | None] = []
        self.transaction = TransactionRecord(
            transaction_id="60000000-0000-0000-0000-000000000001",
            transaction_type="issue",
            occurred_at="2026-07-24T11:33:35+00:00",
            summary=(
                "Issue: 100 each Classic T-Shirt [SHIRT-BLUE-S], "
                "12 each Classic T-Shirt - Red / M [SHIRT-RED-M]"
            ),
        )

    async def read_transactions(
        self,
        *,
        organization_id: UUID,
        query: str | None,
        limit: int,
    ) -> list[TransactionRecord]:
        self.queries.append(query)
        return [] if query is not None else [self.transaction]


class CorrectionTransactionReads(FakeReads):
    async def read_transactions(
        self,
        *,
        organization_id: UUID,
        query: str | None,
        limit: int,
    ) -> list[TransactionRecord]:
        return [
            TransactionRecord(
                transaction_id=str(TRANSACTION_ID),
                transaction_type="receive",
                occurred_at="2026-07-24T12:17:01+00:00",
                summary="Receive: 100 each Industrial Widget [ABC-123]",
            )
        ]


class FakeProposals:
    def __init__(self) -> None:
        self.drafts: list[ProposalDraft] = []

    async def create(self, draft: ProposalDraft) -> UUID:
        self.drafts.append(draft)
        return PROPOSAL_ID


class FakeReversals:
    async def begin(self, *, transaction_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        raise AssertionError("not expected")

    async def capture_reason(
        self,
        *,
        event_id: UUID,
        actor_id: UUID,
        chat_id: int,
        reason: str,
    ) -> UUID | None:
        return None

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        raise AssertionError("not expected")

    async def attach_replacement(
        self,
        *,
        request_id: UUID,
        proposal_id: UUID,
        actor_id: UUID,
    ) -> UUID:
        raise AssertionError("not expected")

    async def get_completed_replacement(
        self,
        *,
        request_id: UUID,
        actor_id: UUID,
    ) -> UUID | None:
        raise AssertionError("not expected")

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        raise AssertionError("not expected")


class LinkedCorrectionReversals(FakeReversals):
    def __init__(self) -> None:
        self.attachments: list[tuple[UUID, UUID, UUID]] = []

    async def begin(self, *, transaction_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        assert transaction_id == TRANSACTION_ID
        return REVERSAL_REQUEST_ID

    async def capture_reason(
        self,
        *,
        event_id: UUID,
        actor_id: UUID,
        chat_id: int,
        reason: str,
    ) -> UUID | None:
        return REVERSAL_REQUEST_ID

    async def attach_replacement(
        self,
        *,
        request_id: UUID,
        proposal_id: UUID,
        actor_id: UUID,
    ) -> UUID:
        self.attachments.append((request_id, proposal_id, actor_id))
        return proposal_id


def production_tools(proposals: FakeProposals) -> ProductionInventoryAgentTools:
    return ProductionInventoryAgentTools(
        context=ProductionToolContext(
            organization_id=ORGANIZATION_ID,
            organization_user_id=ACTOR_ID,
            location_id=LOCATION_ID,
            source_event_id=EVENT_ID,
            external_event_id="telegram-1",
            chat_id=123,
        ),
        catalog=FakeCatalog(),
        reads=FakeReads(),
        proposals=proposals,
        reversals=FakeReversals(),
    )


async def test_exact_sku_read_preserves_identifier_alias_matches() -> None:
    reader = GroundedAgentCatalogReader(
        candidates=AliasCandidateRepository(),
        semantic=None,
        reads=FakeReads(),
        strategy=MatchingStrategy.SEMANTIC,
    )

    records, evidence = await reader.read(
        organization_id=ORGANIZATION_ID,
        location_id=LOCATION_ID,
        arguments=InventoryReadArguments(
            query=None,
            sku="AMOX-500",
            attributes=[],
            include_zero_stock=True,
            limit=5,
        ),
    )

    assert records[0].variant_id == str(VARIANT_ID)
    assert records[0].sku == "MED-AMOX-500"
    assert evidence[VARIANT_ID].match_evidence["matched_value"] == "AMOX-500"


async def test_read_then_add_creates_existing_atomic_proposal_draft() -> None:
    proposals = FakeProposals()
    tools = production_tools(proposals)
    await tools.execute(
        call_id="read",
        name="read_inventory",
        arguments={
            "query": None,
            "sku": "ABC-123",
            "attributes": [],
            "include_zero_stock": True,
            "limit": 5,
        },
    )

    result = json.loads(
        await tools.execute(
            call_id="add",
            name="propose_add_inventory",
            arguments={
                "lines": [
                    {
                        "variant_id": str(VARIANT_ID),
                        "new_item": None,
                        "quantity": 3,
                        "unit": "each",
                        "attributes": [],
                    }
                ],
                "reason": "Delivery",
            },
        )
    )

    assert result["ok"] is True
    assert result["inventory_changed"] is False
    assert tools.stock_proposal_id == PROPOSAL_ID
    assert proposals.drafts[0].lines[0].item_variant_id == VARIANT_ID
    assert proposals.drafts[0].idempotency_key == "telegram:telegram-1:inventory-agent"


async def test_production_tool_rejects_unread_variant() -> None:
    proposals = FakeProposals()
    tools = production_tools(proposals)

    result = json.loads(
        await tools.execute(
            call_id="add",
            name="propose_add_inventory",
            arguments={
                "lines": [
                    {
                        "variant_id": str(VARIANT_ID),
                        "new_item": None,
                        "quantity": 3,
                        "unit": "each",
                        "attributes": [],
                    }
                ],
                "reason": "Delivery",
            },
        )
    )

    assert result["ok"] is False
    assert "not returned by read_inventory" in result["error"]
    assert proposals.drafts == []


async def test_production_tool_requires_fresh_evidence_for_previously_allowed_variant() -> None:
    proposals = FakeProposals()
    tools = ProductionInventoryAgentTools(
        context=ProductionToolContext(
            organization_id=ORGANIZATION_ID,
            organization_user_id=ACTOR_ID,
            location_id=LOCATION_ID,
            source_event_id=EVENT_ID,
            external_event_id="telegram-follow-up",
            chat_id=123,
        ),
        catalog=FakeCatalog(),
        reads=FakeReads(),
        proposals=proposals,
        reversals=FakeReversals(),
        allowed_variant_ids={VARIANT_ID},
    )

    result = json.loads(
        await tools.execute(
            call_id="add-from-old-history",
            name="propose_add_inventory",
            arguments={
                "lines": [
                    {
                        "variant_id": str(VARIANT_ID),
                        "new_item": None,
                        "quantity": 3,
                        "unit": "each",
                        "attributes": [],
                    }
                ],
                "reason": "Delivery follow-up",
            },
        )
    )

    assert result["ok"] is False
    assert "current user message" in result["error"]
    assert proposals.drafts == []


async def test_production_tool_rejects_non_simple_new_item() -> None:
    proposals = FakeProposals()
    tools = production_tools(proposals)

    result = json.loads(
        await tools.execute(
            call_id="add-lot-item",
            name="propose_add_inventory",
            arguments={
                "lines": [
                    {
                        "variant_id": None,
                        "new_item": {
                            "name": "Lot item",
                            "sku": None,
                            "base_unit": "each",
                            "tracking_mode": "lot",
                            "attributes": [],
                        },
                        "quantity": 1,
                        "unit": "each",
                        "attributes": [],
                    }
                ],
                "reason": "Delivery",
            },
        )
    )

    assert result["ok"] is False
    assert "simple tracking only" in result["error"]
    assert proposals.drafts == []


async def test_filtered_transaction_read_includes_recent_fallback_evidence() -> None:
    proposals = FakeProposals()
    reads = FallbackTransactionReads()
    tools = ProductionInventoryAgentTools(
        context=ProductionToolContext(
            organization_id=ORGANIZATION_ID,
            organization_user_id=ACTOR_ID,
            location_id=LOCATION_ID,
            source_event_id=EVENT_ID,
            external_event_id="telegram-correction",
            chat_id=123,
        ),
        catalog=FakeCatalog(),
        reads=reads,
        proposals=proposals,
        reversals=FakeReversals(),
    )

    result = json.loads(
        await tools.execute(
            call_id="read-sale",
            name="read_transactions",
            arguments={"query": "Classic T-Shirt red sale", "limit": 20},
        )
    )

    assert reads.queries == ["Classic T-Shirt red sale", None]
    assert result["targeted_count"] == 0
    assert result["included_recent_fallback"] is True
    assert result["count"] == 1
    assert result["transactions"][0]["transaction_id"] == ("60000000-0000-0000-0000-000000000001")
    assert tools.allowed_transaction_ids == {UUID("60000000-0000-0000-0000-000000000001")}


async def test_correction_reversal_persists_grounded_replacement_for_automatic_follow_up() -> None:
    proposals = FakeProposals()
    reversals = LinkedCorrectionReversals()
    tools = ProductionInventoryAgentTools(
        context=ProductionToolContext(
            organization_id=ORGANIZATION_ID,
            organization_user_id=ACTOR_ID,
            location_id=LOCATION_ID,
            source_event_id=EVENT_ID,
            external_event_id="telegram-correction-linked",
            chat_id=123,
        ),
        catalog=FakeCatalog(),
        reads=CorrectionTransactionReads(),
        proposals=proposals,
        reversals=reversals,
    )
    await tools.execute(
        call_id="read-transaction",
        name="read_transactions",
        arguments={"query": "widget receipt", "limit": 5},
    )
    await tools.execute(
        call_id="read-widget",
        name="read_inventory",
        arguments={
            "query": None,
            "sku": "ABC-123",
            "attributes": [],
            "include_zero_stock": True,
            "limit": 5,
        },
    )

    result = json.loads(
        await tools.execute(
            call_id="correct-receipt",
            name="propose_reversal",
            arguments={
                "transaction_id": str(TRANSACTION_ID),
                "reason": "The correct receipt was 5 widgets",
                "replacement": {
                    "operation": "ADD",
                    "lines": [
                        {
                            "variant_id": str(VARIANT_ID),
                            "new_item": None,
                            "quantity": 5,
                            "unit": "each",
                            "attributes": [],
                        }
                    ],
                    "reason": "Corrected widget receipt",
                },
            },
        )
    )

    assert result["ok"] is True
    assert result["replacement_will_follow_automatically"] is True
    assert result["replacement_proposal_id"] == str(PROPOSAL_ID)
    assert proposals.drafts[0].idempotency_key == (
        "telegram:telegram-correction-linked:inventory-agent-correction-replacement"
    )
    assert proposals.drafts[0].lines[0].requested_quantity == Decimal("5")
    assert reversals.attachments == [(REVERSAL_REQUEST_ID, PROPOSAL_ID, ACTOR_ID)]
