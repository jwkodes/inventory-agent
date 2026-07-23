"""Tests for the Supabase outbound-delivery adapter."""

import json
from uuid import UUID

import httpx

from inventory_agent.processing.models import OutboxCompletionStatus
from inventory_agent.processing.repository import SupabaseProcessingOutboxDeliveryRepository

OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000005")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000005")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000005")
LINE_ID = UUID("41000000-0000-0000-0000-000000000005")


async def test_delivery_repository_claims_and_completes_outcome() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("claim_processing_outbox"):
            return httpx.Response(
                200,
                json=[
                    {
                        "outbox_id": str(OUTBOX_ID),
                        "organization_id": str(ORGANIZATION_ID),
                        "source_event_id": str(EVENT_ID),
                        "outcome_type": "clarification_required",
                        "aggregate_id": None,
                        "chat_id": -100123,
                        "payload": {"message": "Which item?"},
                        "attempt_number": 1,
                    }
                ],
            )
        return httpx.Response(200, json="sent")

    repository = SupabaseProcessingOutboxDeliveryRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    outcome = await repository.claim(OUTBOX_ID)
    completion = await repository.finish(outbox_id=OUTBOX_ID, success=True)

    assert outcome is not None
    assert outcome.payload == {"message": "Which item?"}
    assert completion is OutboxCompletionStatus.SENT
    assert requests == [
        (
            "/rest/v1/rpc/claim_processing_outbox",
            {"p_outbox_id": str(OUTBOX_ID)},
        ),
        (
            "/rest/v1/rpc/finish_processing_outbox",
            {
                "p_outbox_id": str(OUTBOX_ID),
                "p_success": True,
                "p_error_message": None,
                "p_retry_delay_seconds": 30,
            },
        ),
    ]


async def test_delivery_repository_loads_confirmation_view() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("get_proposal_confirmation_view")
        return httpx.Response(
            200,
            json={
                "proposal_id": str(PROPOSAL_ID),
                "intent": "receive_stock",
                "lines": [
                    {
                        "proposal_line_id": str(LINE_ID),
                        "description": "Full Cream Milk 1L",
                        "quantity": "3.00000000",
                        "unit": None,
                        "matched_label": "Full Cream Milk 1L · MILK-FULLCREAM-1L",
                        "candidate_choices": [],
                    }
                ],
            },
        )

    repository = SupabaseProcessingOutboxDeliveryRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    view = await repository.get_proposal_view(PROPOSAL_ID)

    assert view.intent == "receive_stock"
    assert view.lines[0].quantity.as_tuple().exponent == -8
