"""Tests for the atomic proposal RPC adapter."""

import json
from decimal import Decimal
from uuid import UUID

import httpx

from inventory_agent.proposals.models import ProposalDraft, ProposalIntent, ProposalLineDraft
from inventory_agent.proposals.repository import SupabaseProposalRepository

PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")


async def test_repository_serializes_draft_for_atomic_rpc() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/create_inventory_proposal"
        body = json.loads(request.content)
        assert body["p_intent"] == "receive_stock"
        assert body["p_idempotency_key"] == "telegram:9001"
        assert body["p_lines"][0]["requested_quantity"] == "3"
        assert body["p_lines"][0]["item_variant_id"] == ("21000000-0000-0000-0000-000000000001")
        return httpx.Response(200, json=str(PROPOSAL_ID))

    repository = SupabaseProposalRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )
    draft = ProposalDraft(
        organization_id=UUID("10000000-0000-0000-0000-000000000001"),
        location_id=UUID("12000000-0000-0000-0000-000000000001"),
        source_event_id=UUID("50000000-0000-0000-0000-000000000001"),
        created_by=UUID("11000000-0000-0000-0000-000000000001"),
        intent=ProposalIntent.RECEIVE_STOCK,
        idempotency_key="telegram:9001",
        raw_command={"intent": "RECEIVE_STOCK"},
        lines=[
            ProposalLineDraft(
                line_number=1,
                source_text="received three butter",
                requested_quantity=Decimal("3"),
                item_variant_id=UUID("21000000-0000-0000-0000-000000000001"),
                match_method="exact_identifier",
                match_score=Decimal("1"),
            )
        ],
    )

    proposal_id = await repository.create(draft)

    assert proposal_id == PROPOSAL_ID
