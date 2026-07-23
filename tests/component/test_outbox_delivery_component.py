"""Local-Supabase component test for durable Telegram outcome delivery."""

import hashlib
import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest

from inventory_agent.artifacts.repository import SupabaseSourceArtifactRepository
from inventory_agent.catalog.repository import SupabaseCatalogItemCreationRepository
from inventory_agent.config import Settings
from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import ExtractedInventoryCommand
from inventory_agent.matching.repository import SupabaseInventoryCandidateRepository
from inventory_agent.matching.service import InventoryItemMatcher
from inventory_agent.processing.callback_events import TelegramCallbackEventProcessor
from inventory_agent.processing.commands import InventoryCommandHandler
from inventory_agent.processing.delivery import TelegramOutboxDeliveryWorker
from inventory_agent.processing.image_events import TelegramImageEventProcessor
from inventory_agent.processing.models import OutboxDeliveryStatus, TextEventProcessingStatus
from inventory_agent.processing.repository import (
    SupabaseProcessingOutboxDeliveryRepository,
    SupabaseProcessingOutboxRepository,
    SupabaseSourceEventWorkRepository,
)
from inventory_agent.processing.text_events import TelegramTextEventProcessor
from inventory_agent.proposals.actions import SupabaseProposalActionRepository
from inventory_agent.proposals.repository import SupabaseProposalRepository
from inventory_agent.reversals.repository import SupabaseReversalRepository
from inventory_agent.telegram.callback_dispatcher import TelegramCallbackDispatcher
from inventory_agent.telegram.callbacks import CallbackAction, CallbackCommand, encode_callback
from inventory_agent.telegram.client import DownloadedTelegramFile

pytestmark = pytest.mark.component

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")


class RecordingTelegramSender:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.keyboards: list[list[list[dict[str, str]]] | None] = []
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
        self.keyboards.append(inline_keyboard)
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


class UnusedCatalogDetailsInterpreter:
    async def interpret(self, **kwargs: object) -> object:
        raise AssertionError("no catalog request is pending in this component test")


class FixedInvoiceImageInterpreter:
    async def interpret(
        self,
        *,
        image_bytes: bytes,
        media_type: str,
        caption: str | None = None,
    ) -> CommandExtractionResult:
        assert image_bytes == b"component-invoice-image"
        assert media_type == "image/jpeg"
        assert caption == "delivery"
        return CommandExtractionResult(
            command=ExtractedInventoryCommand.model_validate(
                {
                    "schema_version": "1.0",
                    "intent": "RECEIVE_STOCK",
                    "location_hint": None,
                    "lines": [
                        {
                            "source_text": "AMOX-500 3 BOX",
                            "item_reference": {
                                "type": "PART_NUMBER",
                                "value": "AMOX-500",
                            },
                            "description": "amoxicillin",
                            "quantity": "3",
                            "unit": "box",
                            "attributes": [{"key": "expiry_date", "value": "2027-06-30"}],
                        }
                    ],
                    "notes": "component invoice",
                    "needs_clarification": False,
                    "clarification_question": None,
                }
            ),
            response_id="component-image-response",
            model="component-fake-model",
            prompt_version="inventory-invoice-image-v1",
        )


class FixedTelegramImageDownloader:
    async def download_file(
        self,
        *,
        file_id: str,
        expected_size: int | None = None,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> DownloadedTelegramFile:
        assert file_id == "component-photo"
        assert expected_size == len(b"component-invoice-image")
        return DownloadedTelegramFile(
            data=b"component-invoice-image",
            file_path="photos/component.jpg",
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


async def active_telegram_user_id(client: httpx.AsyncClient) -> int:
    response = await client.get(
        "/organization_users",
        params={
            "select": "telegram_user_id",
            "organization_id": f"eq.{ORGANIZATION_ID}",
            "active": "eq.true",
            "limit": "1",
        },
    )
    response.raise_for_status()
    rows = response.json()
    assert isinstance(rows, list) and len(rows) == 1
    telegram_user_id = rows[0]["telegram_user_id"]
    assert isinstance(telegram_user_id, int)
    return telegram_user_id


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
    component_chat_id = -(1_000_000_000 + event_id.int % 1_000_000_000)
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
    async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
        telegram_user_id = await active_telegram_user_id(client)
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
                        "from": {"id": telegram_user_id},
                        "chat": {"id": component_chat_id},
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
                catalog_interpreter=UnusedCatalogDetailsInterpreter(),  # type: ignore[arg-type]
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
                reversals=SupabaseReversalRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                catalog=SupabaseCatalogItemCreationRepository(
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


async def test_image_processing_crosses_storage_and_database_boundaries() -> None:
    settings, secret_key = local_supabase()
    event_id = uuid4()
    image_bytes = b"component-invoice-image"
    digest = hashlib.sha256(image_bytes).hexdigest()
    storage_path = f"{ORGANIZATION_ID}/{event_id}/{digest}.jpg"
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
    async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
        telegram_user_id = await active_telegram_user_id(client)
        create_event = await client.post(
            "/source_events",
            headers={"Prefer": "return=minimal"},
            json={
                "id": str(event_id),
                "organization_id": str(ORGANIZATION_ID),
                "provider": "telegram",
                "external_event_id": f"component-image-{event_id}",
                "event_type": "invoice_image",
                "payload": {
                    "message": {
                        "from": {"id": telegram_user_id},
                        "chat": {"id": telegram_user_id},
                        "caption": "delivery",
                        "photo": [
                            {
                                "file_id": "component-photo",
                                "file_unique_id": "component-photo-unique",
                                "width": 900,
                                "height": 1200,
                                "file_size": len(image_bytes),
                            }
                        ],
                    }
                },
            },
        )
        create_event.raise_for_status()
        try:
            matcher = InventoryItemMatcher(
                repository=SupabaseInventoryCandidateRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                )
            )
            proposals = SupabaseProposalRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            )
            outbox = SupabaseProcessingOutboxRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            )
            processor = TelegramImageEventProcessor(
                events=SupabaseSourceEventWorkRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                downloader=FixedTelegramImageDownloader(),
                artifacts=SupabaseSourceArtifactRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                    bucket=settings.supabase_storage_bucket,
                ),
                interpreter=FixedInvoiceImageInterpreter(),
                commands=InventoryCommandHandler(
                    matcher=matcher,
                    proposals=proposals,
                    outbox=outbox,
                ),
            )

            result = await processor.process_next()

            assert result is not None
            assert result.status is TextEventProcessingStatus.PROPOSAL_READY
            artifact = await client.get(
                "/source_artifacts",
                params={
                    "select": "storage_bucket,storage_path,media_type,sha256",
                    "source_event_id": f"eq.{event_id}",
                },
            )
            artifact.raise_for_status()
            assert artifact.json() == [
                {
                    "storage_bucket": settings.supabase_storage_bucket,
                    "storage_path": storage_path,
                    "media_type": "image/jpeg",
                    "sha256": digest,
                }
            ]
            proposal = await client.get(
                "/transaction_proposals",
                params={
                    "select": "prompt_version,proposal_lines(item_variant_id,attributes)",
                    "id": f"eq.{result.proposal_id}",
                },
            )
            proposal.raise_for_status()
            assert proposal.json()[0]["prompt_version"] == "inventory-invoice-image-v1"
            assert proposal.json()[0]["proposal_lines"][0]["item_variant_id"] == (
                "21000000-0000-0000-0000-000000000003"
            )
        finally:
            delete_proposal = await client.delete(
                "/transaction_proposals",
                params={"source_event_id": f"eq.{event_id}"},
            )
            delete_proposal.raise_for_status()
            delete_artifact = await client.delete(
                "/source_artifacts",
                params={"source_event_id": f"eq.{event_id}"},
            )
            delete_artifact.raise_for_status()
            async with httpx.AsyncClient(
                base_url=settings.supabase_url,
                headers=headers,
            ) as storage_client:
                delete_object = await storage_client.request(
                    "DELETE",
                    f"/storage/v1/object/{settings.supabase_storage_bucket}",
                    json={"prefixes": [storage_path]},
                )
                delete_object.raise_for_status()
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
        telegram_user_id = await active_telegram_user_id(client)
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
                            "from": {"id": telegram_user_id},
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
                    reversals=SupabaseReversalRepository(
                        supabase_url=settings.supabase_url,
                        secret_key=secret_key,
                    ),
                    catalog=SupabaseCatalogItemCreationRepository(
                        supabase_url=settings.supabase_url,
                        secret_key=secret_key,
                    ),
                ),
                message_editor=telegram,
                outbox=SupabaseProcessingOutboxRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
            )
            result = await processor.process_next()

            assert result is not None
            assert result.outcome.action is CallbackAction.CANCEL_PROPOSAL
            assert telegram.answers == [f"query-{callback_event_id}"]
            assert telegram.edits == [(100000001, 77, "Proposal cancelled.")]

            callback_outbox = await client.get(
                "/processing_outbox",
                params={
                    "select": "id,status,outcome_type",
                    "source_event_id": f"eq.{callback_event_id}",
                },
            )
            callback_outbox.raise_for_status()
            outbox_row = callback_outbox.json()[0]
            assert outbox_row["status"] == "pending"
            assert outbox_row["outcome_type"] == "callback_notice"

            delivery = await TelegramOutboxDeliveryWorker(
                repository=SupabaseProcessingOutboxDeliveryRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                sender=telegram,
            ).deliver_one(UUID(outbox_row["id"]))
            assert delivery.status is OutboxDeliveryStatus.SENT
            assert telegram.messages == [(100000001, "Proposal cancelled.")]

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


async def test_reversal_reason_outbox_delivers_as_a_separate_message() -> None:
    settings, secret_key = local_supabase()
    event_id = uuid4()
    outbox_id = uuid4()
    reversal_request_id = uuid4()
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
    async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
        telegram_user_id = await active_telegram_user_id(client)
        create_event = await client.post(
            "/source_events",
            headers={"Prefer": "return=minimal"},
            json={
                "id": str(event_id),
                "organization_id": str(ORGANIZATION_ID),
                "provider": "component_test",
                "external_event_id": f"component-reversal-reason-{event_id}",
                "event_type": "callback_query",
                "status": "processed",
                "processed_at": datetime.now(UTC).isoformat(),
            },
        )
        create_event.raise_for_status()
        create_outbox = await client.post(
            "/processing_outbox",
            headers={"Prefer": "return=minimal"},
            json={
                "id": str(outbox_id),
                "organization_id": str(ORGANIZATION_ID),
                "source_event_id": str(event_id),
                "outcome_type": "reversal_reason_required",
                "aggregate_id": str(reversal_request_id),
                "chat_id": telegram_user_id,
                "payload": {},
            },
        )
        create_outbox.raise_for_status()

        try:
            telegram = RecordingTelegramSender()
            delivery_result = await TelegramOutboxDeliveryWorker(
                repository=SupabaseProcessingOutboxDeliveryRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                sender=telegram,
            ).deliver_one(outbox_id)

            assert delivery_result.status is OutboxDeliveryStatus.SENT
            assert len(telegram.messages) == 1
            assert telegram.messages[0][0] == telegram_user_id
            assert "Reply with the reason" in telegram.messages[0][1]
            assert telegram.keyboards[0] is not None
            stored = await client.get(
                "/processing_outbox",
                params={"select": "status,attempts", "id": f"eq.{outbox_id}"},
            )
            stored.raise_for_status()
            assert stored.json() == [{"status": "sent", "attempts": 1}]
        finally:
            cleanup = await client.delete(
                "/source_events",
                params={"id": f"eq.{event_id}"},
            )
            cleanup.raise_for_status()
