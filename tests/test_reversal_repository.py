"""Contract tests for Supabase reversal RPC calls."""

import json
from uuid import UUID

import httpx

from inventory_agent.reversals.repository import SupabaseReversalRepository

ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("70000000-0000-0000-0000-000000000001")
REVERSAL_ID = UUID("60000000-0000-0000-0000-000000000002")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")


async def test_reversal_repository_maps_conversation_and_action_rpcs() -> None:
    requests: list[tuple[str, dict[str, object]]] = []
    results = {
        "/rest/v1/rpc/begin_transaction_reversal_request": str(REQUEST_ID),
        "/rest/v1/rpc/capture_transaction_reversal_reason": str(REQUEST_ID),
        "/rest/v1/rpc/confirm_transaction_reversal_request": str(REVERSAL_ID),
        "/rest/v1/rpc/attach_transaction_reversal_replacement": str(PROPOSAL_ID),
        "/rest/v1/rpc/get_completed_reversal_replacement": str(PROPOSAL_ID),
        "/rest/v1/rpc/cancel_transaction_reversal_request": str(REQUEST_ID),
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(200, json=results[request.url.path])

    repository = SupabaseReversalRepository(
        supabase_url="http://supabase.test",
        secret_key="secret",
        transport=httpx.MockTransport(handle_request),
    )

    assert (
        await repository.begin(
            transaction_id=TRANSACTION_ID,
            actor_id=ACTOR_ID,
            chat_id=-100123,
        )
        == REQUEST_ID
    )
    assert (
        await repository.capture_reason(
            event_id=EVENT_ID,
            actor_id=ACTOR_ID,
            chat_id=-100123,
            reason="Duplicate receipt",
        )
        == REQUEST_ID
    )
    assert await repository.confirm(request_id=REQUEST_ID, actor_id=ACTOR_ID) == REVERSAL_ID
    assert (
        await repository.attach_replacement(
            request_id=REQUEST_ID,
            proposal_id=PROPOSAL_ID,
            actor_id=ACTOR_ID,
        )
        == PROPOSAL_ID
    )
    assert (
        await repository.get_completed_replacement(
            request_id=REQUEST_ID,
            actor_id=ACTOR_ID,
        )
        == PROPOSAL_ID
    )
    assert await repository.cancel(request_id=REQUEST_ID, actor_id=ACTOR_ID) == REQUEST_ID

    assert requests[0][1]["p_transaction_id"] == str(TRANSACTION_ID)
    assert requests[1][1]["p_reason"] == "Duplicate receipt"


async def test_capture_reason_returns_none_when_no_conversation_is_waiting() -> None:
    repository = SupabaseReversalRepository(
        supabase_url="http://supabase.test",
        secret_key="secret",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=b"null", headers={"content-type": "application/json"}
            )
        ),
    )

    result = await repository.capture_reason(
        event_id=EVENT_ID,
        actor_id=ACTOR_ID,
        chat_id=-100123,
        reason="ordinary inventory message",
    )

    assert result is None
