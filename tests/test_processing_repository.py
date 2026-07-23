"""Tests for source-work and durable-outbox Supabase adapters."""

import json
from uuid import UUID

import httpx

from inventory_agent.processing.models import ProcessingOutcomeDraft, ProcessingOutcomeType
from inventory_agent.processing.repository import (
    SupabaseProcessingOutboxRepository,
    SupabaseSourceEventWorkRepository,
)

EVENT_ID = UUID("50000000-0000-0000-0000-000000000004")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
MEMBER_ID = UUID("11000000-0000-0000-0000-000000000001")
LOCATION_ID = UUID("12000000-0000-0000-0000-000000000001")
OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000001")


async def test_source_repository_claims_and_completes_event() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("claim_telegram_text_event"):
            return httpx.Response(
                200,
                json=[
                    {
                        "event_id": str(EVENT_ID),
                        "organization_id": str(ORGANIZATION_ID),
                        "organization_user_id": str(MEMBER_ID),
                        "location_id": str(LOCATION_ID),
                        "external_event_id": "70004",
                        "chat_id": -100123,
                        "telegram_user_id": 100000001,
                        "message_text": "received three AMOX-500",
                    }
                ],
            )
        return httpx.Response(200, json=True)

    repository = SupabaseSourceEventWorkRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    context = await repository.claim_text_event(EVENT_ID)
    finished = await repository.finish_event(event_id=EVENT_ID, success=True)

    assert context is not None
    assert context.organization_user_id == MEMBER_ID
    assert context.message_text == "received three AMOX-500"
    assert finished is True
    assert requests == [
        ("/rest/v1/rpc/claim_telegram_text_event", {"p_event_id": str(EVENT_ID)}),
        (
            "/rest/v1/rpc/finish_source_event",
            {
                "p_event_id": str(EVENT_ID),
                "p_success": True,
                "p_error_message": None,
            },
        ),
    ]


async def test_source_repository_returns_none_when_claim_has_no_rows() -> None:
    repository = SupabaseSourceEventWorkRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
    )

    assert await repository.claim_text_event(EVENT_ID) is None


async def test_source_repository_claims_next_event_without_an_id() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/claim_next_telegram_text_event"
        assert json.loads(request.content) == {}
        return httpx.Response(
            200,
            json=[
                {
                    "event_id": str(EVENT_ID),
                    "organization_id": str(ORGANIZATION_ID),
                    "organization_user_id": str(MEMBER_ID),
                    "location_id": str(LOCATION_ID),
                    "external_event_id": "70004",
                    "chat_id": -100123,
                    "telegram_user_id": 100000001,
                    "message_text": "received three AMOX-500",
                }
            ],
        )

    repository = SupabaseSourceEventWorkRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    context = await repository.claim_next_text_event()

    assert context is not None
    assert context.event_id == EVENT_ID


async def test_source_repository_claims_next_callback_context() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/claim_next_telegram_callback_event"
        return httpx.Response(
            200,
            json=[
                {
                    "event_id": str(EVENT_ID),
                    "organization_id": str(ORGANIZATION_ID),
                    "organization_user_id": str(MEMBER_ID),
                    "external_event_id": "70005",
                    "callback_query_id": "callback-5",
                    "callback_data": "opaque-data",
                    "chat_id": -100123,
                    "telegram_message_id": 77,
                    "telegram_user_id": 100000001,
                }
            ],
        )

    repository = SupabaseSourceEventWorkRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    context = await repository.claim_next_callback_event()

    assert context is not None
    assert context.callback_query_id == "callback-5"
    assert context.telegram_message_id == 77


async def test_source_repository_claims_next_invoice_image_context() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/claim_next_telegram_image_event"
        return httpx.Response(
            200,
            json=[
                {
                    "event_id": str(EVENT_ID),
                    "organization_id": str(ORGANIZATION_ID),
                    "organization_user_id": str(MEMBER_ID),
                    "location_id": str(LOCATION_ID),
                    "external_event_id": "70006",
                    "chat_id": -100123,
                    "telegram_user_id": 100000001,
                    "telegram_file_id": "large-photo",
                    "telegram_file_unique_id": "photo-unique",
                    "media_type": "image/jpeg",
                    "original_file_name": None,
                    "file_size": 1234,
                    "width": 900,
                    "height": 1200,
                    "caption": "Supplier delivery",
                }
            ],
        )

    repository = SupabaseSourceEventWorkRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    context = await repository.claim_next_image_event()

    assert context is not None
    assert context.telegram_file_id == "large-photo"
    assert context.media_type == "image/jpeg"
    assert context.caption == "Supplier delivery"


async def test_outbox_repository_serializes_durable_outcome() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/enqueue_processing_outcome"
        assert json.loads(request.content) == {
            "p_organization_id": str(ORGANIZATION_ID),
            "p_source_event_id": str(EVENT_ID),
            "p_outcome_type": "clarification_required",
            "p_aggregate_id": None,
            "p_chat_id": -100123,
            "p_payload": {"message": "Which item?"},
        }
        return httpx.Response(200, json=str(OUTBOX_ID))

    repository = SupabaseProcessingOutboxRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )
    result = await repository.enqueue(
        ProcessingOutcomeDraft(
            organization_id=ORGANIZATION_ID,
            source_event_id=EVENT_ID,
            outcome_type=ProcessingOutcomeType.CLARIFICATION_REQUIRED,
            chat_id=-100123,
            payload={"message": "Which item?"},
        )
    )

    assert result == OUTBOX_ID
