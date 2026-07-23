"""Local-Supabase component test for durable Telegram outcome delivery."""

import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest

from inventory_agent.config import Settings
from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import ExtractedInventoryCommand
from inventory_agent.matching.repository import SupabaseInventoryCandidateRepository
from inventory_agent.matching.service import InventoryItemMatcher
from inventory_agent.processing.callback_events import TelegramCallbackEventProcessor
from inventory_agent.processing.delivery import TelegramOutboxDeliveryWorker
from inventory_agent.processing.models import OutboxDeliveryStatus, TextEventProcessingStatus
from inventory_agent.processing.repository import (
    SupabaseProcessingOutboxDeliveryRepository,
    SupabaseProcessingOutboxRepository,
    SupabaseSourceEventWorkRepository,
)
from inventory_agent.processing.text_events import TelegramTextEventProcessor
from inventory_agent.proposals.actions import SupabaseProposalActionRepository
from inventory_agent.proposals.repository import SupabaseProposalRepository
from inventory_agent.telegram.callback_dispatcher import TelegramCallbackDispatcher
from inventory_agent.telegram.callbacks import CallbackAction, CallbackCommand, encode_callback

pytestmark = pytest.mark.component

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")


class RecordingTelegramSender:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.answers: list[str] = []
        self.edits: list[tuple[int, int, str]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        self.messages.append((chat_id, text))
        return 991

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        self.answers.append(callback_query_id)

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.edits.append((chat_id, message_id, text))


class FixedCommandInterpreter:
    async def interpret(self, user_text: str) -> CommandExtractionResult:
        assert user_text == "received three AMOX-500"
        command = ExtractedInventoryCommand.model_validate(
            {
                "schema_version": "1.0",
                "intent": "RECEIVE_STOCK",
                "location_hint": None,
                "lines": [
                    {
                        "source_text": "three AMOX-500",
                        "item_reference": {"type": "PART_NUMBER", "value": "AMOX-500"},
                        "description": "amoxicillin",
                        "quantity": "3",
                        "unit": "box",
                        "attributes": [{"key": "expiry_date", "value": "2027-06-30"}],
                    }
                ],
                "notes": None,
                "needs_clarification": False,
                "clarification_question": None,
            }
        )
        return CommandExtractionResult(
            command=command,
            response_id="component-response",
            model="component-fake-model",
        )


def local_supabase() -> tuple[Settings, str]:
    if os.getenv("RUN_COMPONENT_TESTS") != "1":
        pytest.skip("set RUN_COMPONENT_TESTS=1 to run local infrastructure tests")

    settings = Settings()
    hostname = urlparse(settings.supabase_url).hostname
    if hostname not in {"127.0.0.1", "localhost"}:
        pytest.fail("component tests only run against local Supabase")
    secret = settings.supabase_secret_key
    secret_key = secret.get_secret_value() if secret is not None else ""
    if not secret_key:
        pytest.skip("SUPABASE_SECRET_KEY is not configured in .env")
    return settings, secret_key


async def test_delivery_crosses_python_and_local_supabase_boundaries() -> None:
    settings, secret_key = local_supabase()

    event_id = uuid4()
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
    async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
        create_event = await client.post(
            "/source_events",
            headers={"Prefer": "return=minimal"},
            json={
                "id": str(event_id),
                "organization_id": str(ORGANIZATION_ID),
                "provider": "component_test",
                "external_event_id": f"component-{event_id}",
                "event_type": "message",
                "status": "processed",
                "processed_at": datetime.now(UTC).isoformat(),
            },
        )
        create_event.raise_for_status()
        try:
            enqueue = await client.post(
                "/rpc/enqueue_processing_outcome",
                json={
                    "p_organization_id": str(ORGANIZATION_ID),
                    "p_source_event_id": str(event_id),
                    "p_outcome_type": "clarification_required",
                    "p_aggregate_id": None,
                    "p_chat_id": 100000001,
                    "p_payload": {"message": "Which item should I use?"},
                },
            )
            enqueue.raise_for_status()
            outbox_id = UUID(enqueue.json())

            sender = RecordingTelegramSender()
            worker = TelegramOutboxDeliveryWorker(
                repository=SupabaseProcessingOutboxDeliveryRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                sender=sender,
            )
            result = await worker.deliver_one(outbox_id)

            assert result.status is OutboxDeliveryStatus.SENT
            assert result.telegram_message_id == 991
            assert sender.messages == [(100000001, "Which item should I use?")]

            stored = await client.get(
                "/processing_outbox",
                params={"select": "status,attempts,sent_at", "id": f"eq.{outbox_id}"},
            )
            stored.raise_for_status()
            assert stored.json()[0]["status"] == "sent"
            assert stored.json()[0]["attempts"] == 1
            assert stored.json()[0]["sent_at"] is not None
        finally:
            cleanup = await client.delete(
                "/source_events",
                params={"id": f"eq.{event_id}"},
            )
            cleanup.raise_for_status()


async def test_text_processing_crosses_python_and_local_supabase_boundaries() -> None:
    settings, secret_key = local_supabase()
    event_id = uuid4()
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
    async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
        create_event = await client.post(
            "/source_events",
            headers={"Prefer": "return=minimal"},
            json={
                "id": str(event_id),
                "organization_id": str(ORGANIZATION_ID),
                "provider": "telegram",
                "external_event_id": f"component-processing-{event_id}",
                "event_type": "message",
                "payload": {
                    "update_id": 88001,
                    "message": {
                        "message_id": 88,
                        "from": {"id": 100000001},
                        "chat": {"id": 100000001},
                        "text": "received three AMOX-500",
                    },
                },
            },
        )
        create_event.raise_for_status()
        try:
            processor = TelegramTextEventProcessor(
                events=SupabaseSourceEventWorkRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                interpreter=FixedCommandInterpreter(),
                matcher=InventoryItemMatcher(
                    repository=SupabaseInventoryCandidateRepository(
                        supabase_url=settings.supabase_url,
                        secret_key=secret_key,
                    )
                ),
                proposals=SupabaseProposalRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                outbox=SupabaseProcessingOutboxRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
            )

            result = await processor.process_next()

            assert result is not None
            assert result.status is TextEventProcessingStatus.PROPOSAL_READY
            assert result.proposal_id is not None
            proposal = await client.get(
                "/transaction_proposals",
                params={
                    "select": (
                        "status,intent,proposal_lines("
                        "item_variant_id,base_quantity_delta,attributes)"
                    ),
                    "id": f"eq.{result.proposal_id}",
                },
            )
            proposal.raise_for_status()
            stored = proposal.json()[0]
            assert stored["status"] == "pending_confirmation"
            assert stored["intent"] == "receive_stock"
            assert stored["proposal_lines"][0]["item_variant_id"] == (
                "21000000-0000-0000-0000-000000000003"
            )
            assert stored["proposal_lines"][0]["base_quantity_delta"] == 3
            assert stored["proposal_lines"][0]["attributes"] == {"expiry_date": "2027-06-30"}

            outbox = await client.get(
                "/processing_outbox",
                params={
                    "select": "status,outcome_type,aggregate_id",
                    "source_event_id": f"eq.{event_id}",
                },
            )
            outbox.raise_for_status()
            assert outbox.json() == [
                {
                    "status": "pending",
                    "outcome_type": "proposal_ready",
                    "aggregate_id": str(result.proposal_id),
                }
            ]
        finally:
            delete_proposal = await client.delete(
                "/transaction_proposals",
                params={"source_event_id": f"eq.{event_id}"},
            )
            delete_proposal.raise_for_status()
            cleanup = await client.delete(
                "/source_events",
                params={"id": f"eq.{event_id}"},
            )
            cleanup.raise_for_status()


async def test_callback_processing_crosses_python_and_local_supabase_boundaries() -> None:
    settings, secret_key = local_supabase()
    proposal_event_id = uuid4()
    callback_event_id = uuid4()
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
    async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
        proposal_source = await client.post(
            "/source_events",
            headers={"Prefer": "return=minimal"},
            json={
                "id": str(proposal_event_id),
                "organization_id": str(ORGANIZATION_ID),
                "provider": "component_test",
                "external_event_id": f"component-proposal-{proposal_event_id}",
                "event_type": "message",
                "status": "processed",
                "processed_at": datetime.now(UTC).isoformat(),
            },
        )
        proposal_source.raise_for_status()
        proposal_id: UUID | None = None
        try:
            create_proposal = await client.post(
                "/rpc/create_inventory_proposal",
                json={
                    "p_organization_id": str(ORGANIZATION_ID),
                    "p_location_id": "12000000-0000-0000-0000-000000000001",
                    "p_source_event_id": str(proposal_event_id),
                    "p_created_by": "11000000-0000-0000-0000-000000000001",
                    "p_intent": "receive_stock",
                    "p_idempotency_key": f"component-callback-{proposal_event_id}",
                    "p_raw_command": {},
                    "p_model_name": None,
                    "p_model_response_id": None,
                    "p_prompt_version": None,
                    "p_notes": None,
                    "p_lines": [
                        {
                            "line_number": 1,
                            "source_text": "three milk",
                            "requested_quantity": 3,
                            "item_variant_id": ("21000000-0000-0000-0000-000000000002"),
                            "match_method": "exact_identifier",
                            "match_score": 1,
                        }
                    ],
                },
            )
            create_proposal.raise_for_status()
            proposal_id = UUID(create_proposal.json())
            callback_data = encode_callback(
                CallbackCommand(CallbackAction.CANCEL_PROPOSAL, proposal_id)
            )
            create_callback = await client.post(
                "/source_events",
                headers={"Prefer": "return=minimal"},
                json={
                    "id": str(callback_event_id),
                    "organization_id": str(ORGANIZATION_ID),
                    "provider": "telegram",
                    "external_event_id": f"component-callback-{callback_event_id}",
                    "event_type": "callback_query",
                    "payload": {
                        "callback_query": {
                            "id": f"query-{callback_event_id}",
                            "from": {"id": 100000001},
                            "data": callback_data,
                            "message": {
                                "message_id": 77,
                                "chat": {"id": 100000001},
                            },
                        }
                    },
                },
            )
            create_callback.raise_for_status()

            telegram = RecordingTelegramSender()
            processor = TelegramCallbackEventProcessor(
                events=SupabaseSourceEventWorkRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                dispatcher=TelegramCallbackDispatcher(
                    answerer=telegram,
                    repository=SupabaseProposalActionRepository(
                        supabase_url=settings.supabase_url,
                        secret_key=secret_key,
                    ),
                ),
                proposal_views=SupabaseProcessingOutboxDeliveryRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                message_editor=telegram,
            )
            result = await processor.process_next()

            assert result is not None
            assert result.outcome.action is CallbackAction.CANCEL_PROPOSAL
            assert telegram.answers == [f"query-{callback_event_id}"]
            assert telegram.edits == [(100000001, 77, "Proposal cancelled.")]

            proposal = await client.get(
                "/transaction_proposals",
                params={"select": "status", "id": f"eq.{proposal_id}"},
            )
            proposal.raise_for_status()
            assert proposal.json() == [{"status": "rejected"}]
            callback = await client.get(
                "/source_events",
                params={"select": "status", "id": f"eq.{callback_event_id}"},
            )
            callback.raise_for_status()
            assert callback.json() == [{"status": "processed"}]
        finally:
            if proposal_id is not None:
                delete_proposal = await client.delete(
                    "/transaction_proposals",
                    params={"id": f"eq.{proposal_id}"},
                )
                delete_proposal.raise_for_status()
            delete_callback = await client.delete(
                "/source_events",
                params={"id": f"eq.{callback_event_id}"},
            )
            delete_callback.raise_for_status()
            cleanup = await client.delete(
                "/source_events",
                params={"id": f"eq.{proposal_event_id}"},
            )
            cleanup.raise_for_status()
