"""Tests for Supabase proposal action RPC routing."""

import json
from uuid import UUID

import httpx

from inventory_agent.proposals.actions import SupabaseProposalActionRepository

ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")


async def test_confirm_calls_atomic_apply_function() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/apply_inventory_proposal"
        assert json.loads(request.content) == {
            "p_proposal_id": str(PROPOSAL_ID),
            "p_actor_id": str(ACTOR_ID),
        }
        return httpx.Response(200, json="60000000-0000-0000-0000-000000000001")

    repository = SupabaseProposalActionRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    transaction_id = await repository.confirm(proposal_id=PROPOSAL_ID, actor_id=ACTOR_ID)

    assert transaction_id == UUID("60000000-0000-0000-0000-000000000001")
