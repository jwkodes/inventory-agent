"""Local-Supabase component test for durable Telegram outcome delivery."""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest

from inventory_agent.agent.production_tools import (
    GroundedAgentCatalogReader,
)
from inventory_agent.agent.repository import SupabaseAgentRepository
from inventory_agent.agent.runtime import FunctionCall, ModelTurn
from inventory_agent.artifacts.repository import SupabaseSourceArtifactRepository
from inventory_agent.catalog.repository import SupabaseCatalogItemCreationRepository
from inventory_agent.config import Settings
from inventory_agent.dashboard.repository import DashboardRepository
from inventory_agent.extraction.clarification import SupabaseCommandClarificationRepository
from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import ExtractedInventoryCommand, InventoryIntent
from inventory_agent.matching.clarification import SupabaseMatchClarificationRepository
from inventory_agent.matching.judge import CandidateJudgeOutput
from inventory_agent.matching.models import MatchDecision, MatchDecisionStatus
from inventory_agent.matching.repository import SupabaseInventoryCandidateRepository
from inventory_agent.matching.service import InventoryItemMatcher, MatchingStrategy
from inventory_agent.processing.agent_text_events import TelegramAgentTextEventProcessor
from inventory_agent.processing.callback_events import TelegramCallbackEventProcessor
from inventory_agent.processing.commands import InventoryCommandHandler
from inventory_agent.processing.delivery import TelegramOutboxDeliveryWorker
from inventory_agent.processing.image_events import TelegramImageEventProcessor
from inventory_agent.processing.models import (
    OutboxDeliveryStatus,
    TelegramTextEventContext,
    TextEventProcessingStatus,
)
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
from inventory_agent.telegram.registration import (
    RegistrationApplicant,
    SupabaseRegistrationRepository,
    TelegramRegistrationNotificationWorker,
    hash_invite_code,
)

pytestmark = pytest.mark.component

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")


class RecordingTelegramSender:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.keyboards: list[list[list[dict[str, str]]] | None] = []
        self.answers: list[str] = []
        self.removed_keyboards: list[tuple[int, int]] = []

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

    async def remove_inline_keyboard(self, *, chat_id: int, message_id: int) -> None:
        self.removed_keyboards.append((chat_id, message_id))


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


class ExactReceiptAgentModel:
    """Exercise the real agent tool loop without calling OpenAI in CI."""

    def __init__(self) -> None:
        self.calls = 0

    async def respond(
        self,
        *,
        input_items: list[dict[str, object]],
        instructions: str,
        tools: list[dict[str, object]],
    ) -> ModelTurn:
        self.calls += 1
        if self.calls == 1:
            call = FunctionCall(
                call_id="component-read",
                name="read_inventory",
                arguments={
                    "query": None,
                    "sku": "AMOX-500",
                    "attributes": [],
                    "include_zero_stock": True,
                    "limit": 5,
                },
            )
            return _agent_tool_turn(self.calls, call)
        if self.calls == 2:
            call = FunctionCall(
                call_id="component-add",
                name="propose_add_inventory",
                arguments={
                    "lines": [
                        {
                            "variant_id": "21000000-0000-0000-0000-000000000003",
                            "new_item": None,
                            "quantity": 3,
                            "unit": "box",
                            "attributes": [{"key": "expiry_date", "value": "2027-06-30"}],
                        }
                    ],
                    "reason": "Telegram delivery",
                },
            )
            return _agent_tool_turn(self.calls, call)
        tool_outputs = [item for item in input_items if item.get("type") == "function_call_output"]
        assert len(tool_outputs) == 2
        assert '"ok":true' in str(tool_outputs[-1]["output"])
        assert '"proposal_id":' in str(tool_outputs[-1]["output"])
        return ModelTurn(
            response_id="component-agent-final",
            model="component-agent-model",
            output_items=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "I prepared the receipt. Please confirm it.",
                        }
                    ],
                }
            ],
            output_text="I prepared the receipt. Please confirm it.",
            function_calls=[],
        )


def _agent_tool_turn(number: int, call: FunctionCall) -> ModelTurn:
    return ModelTurn(
        response_id=f"component-agent-{number}",
        model="component-agent-model",
        output_items=[
            {
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments),
            }
        ],
        output_text="",
        function_calls=[call],
    )


class UnusedCatalogDetailsInterpreter:
    async def interpret(self, **kwargs: object) -> object:
        raise AssertionError("no catalog request is pending in this component test")


class SelectRedVariantJudge:
    async def judge(self, **kwargs: object) -> CandidateJudgeOutput:
        assert kwargs["clarification_replies"] == ["It is red and medium."]
        return CandidateJudgeOutput(
            action="SELECT",
            selected_candidate_id=UUID("21000000-0000-0000-0000-000000000004"),
            question=None,
            reason="Red and medium identify the red medium variant.",
            resolved_attributes=[
                {"key": "colour", "value": "red"},
                {"key": "size", "value": "M"},
            ],
        )


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


class NoMatchComponentMatcher:
    async def match_line(self, **kwargs: object) -> MatchDecision:
        return MatchDecision(
            status=MatchDecisionStatus.NOT_FOUND,
            selected=None,
            candidates=[],
            reason="No component catalog match.",
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


async def test_registration_rejection_notifies_before_deleting_applicant() -> None:
    settings, secret_key = local_supabase()
    test_id = uuid4()
    invite_code = f"component-{test_id}"
    telegram_user_id = 1_500_000_000 + test_id.int % 500_000_000
    dashboard = DashboardRepository(
        supabase_url=settings.supabase_url,
        secret_key=secret_key,
    )
    registrations = SupabaseRegistrationRepository(
        supabase_url=settings.supabase_url,
        secret_key=secret_key,
    )
    sender = RecordingTelegramSender()
    delivery = TelegramRegistrationNotificationWorker(
        repository=registrations,
        sender=sender,
    )
    invite: dict[str, object] | None = None
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"

    try:
        invite = await dashboard.create_registration_invite(
            organization_id=ORGANIZATION_ID,
            code_hash=hash_invite_code(invite_code),
            code_hint=invite_code[-6:],
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            max_uses=1,
        )
        submission = await registrations.submit_registration(
            invite_code_hash=hash_invite_code(invite_code),
            applicant=RegistrationApplicant(
                telegram_user_id=telegram_user_id,
                telegram_username="component_candidate",
                display_name="Component Candidate",
                private_chat_id=telegram_user_id,
            ),
        )
        assert submission.status == "pending"
        assert submission.request_id is not None

        pending_delivery = await delivery.deliver_one()
        assert pending_delivery.status is OutboxDeliveryStatus.SENT
        assert "Registration submitted" in sender.messages[-1][1]

        rejection = await dashboard.reject_registration(
            organization_id=ORGANIZATION_ID,
            registration_request_id=submission.request_id,
        )
        assert rejection["status"] == "rejection_notifying"
        before_delivery = await dashboard.get_membership_administration(
            organization_id=ORGANIZATION_ID
        )
        assert any(
            row["telegram_user_id"] == telegram_user_id
            for row in before_delivery["requests"]  # type: ignore[union-attr]
        )

        rejection_delivery = await delivery.deliver_one()
        assert rejection_delivery.status is OutboxDeliveryStatus.SENT
        assert "Registration not approved" in sender.messages[-1][1]

        after_delivery = await dashboard.get_membership_administration(
            organization_id=ORGANIZATION_ID
        )
        assert all(
            row["telegram_user_id"] != telegram_user_id
            for row in after_delivery["requests"]  # type: ignore[union-attr]
        )
    finally:
        async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
            notification_cleanup = await client.delete(
                "/registration_telegram_notifications",
                params={"chat_id": f"eq.{telegram_user_id}"},
            )
            notification_cleanup.raise_for_status()
            request_cleanup = await client.delete(
                "/organization_registration_requests",
                params={"telegram_user_id": f"eq.{telegram_user_id}"},
            )
            request_cleanup.raise_for_status()
            if invite is not None:
                invite_cleanup = await client.delete(
                    "/organization_registration_invites",
                    params={"id": f"eq.{invite['id']}"},
                )
                invite_cleanup.raise_for_status()


async def test_linked_correction_replacement_is_persisted_and_cancelled_safely() -> None:
    settings, secret_key = local_supabase()
    event_id = uuid4()
    proposal_id = uuid4()
    proposal_line_id = uuid4()
    reversal_request_id: UUID | None = None
    component_chat_id = -(1_000_000_000 + event_id.int % 1_000_000_000)
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"

    async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
        location_response = await client.get(
            "/locations",
            params={
                "select": "id",
                "organization_id": f"eq.{ORGANIZATION_ID}",
                "active": "eq.true",
                "limit": "1",
            },
        )
        location_response.raise_for_status()
        location_id = location_response.json()[0]["id"]
        variant_response = await client.get(
            "/item_variants",
            params={
                "select": "id",
                "organization_id": f"eq.{ORGANIZATION_ID}",
                "active": "eq.true",
                "limit": "1",
            },
        )
        variant_response.raise_for_status()
        variant_id = variant_response.json()[0]["id"]
        actor_response = await client.get(
            "/organization_users",
            params={"select": "telegram_user_id", "id": f"eq.{ACTOR_ID}"},
        )
        actor_response.raise_for_status()
        telegram_user_id = actor_response.json()[0]["telegram_user_id"]

        request_response = await client.get(
            "/transaction_reversal_requests",
            params={"select": "transaction_id"},
        )
        request_response.raise_for_status()
        unavailable_transaction_ids = {row["transaction_id"] for row in request_response.json()}
        reversal_response = await client.get(
            "/inventory_transactions",
            params={
                "select": "reversal_of_transaction_id",
                "organization_id": f"eq.{ORGANIZATION_ID}",
                "transaction_type": "eq.reversal",
            },
        )
        reversal_response.raise_for_status()
        unavailable_transaction_ids.update(
            row["reversal_of_transaction_id"]
            for row in reversal_response.json()
            if row["reversal_of_transaction_id"] is not None
        )
        transaction_response = await client.get(
            "/inventory_transactions",
            params={
                "select": "id",
                "organization_id": f"eq.{ORGANIZATION_ID}",
                "transaction_type": "neq.reversal",
                "order": "applied_at.desc",
                "limit": "100",
            },
        )
        transaction_response.raise_for_status()
        transaction_id = next(
            (
                UUID(row["id"])
                for row in transaction_response.json()
                if row["id"] not in unavailable_transaction_ids
            ),
            None,
        )
        if transaction_id is None:
            pytest.skip("local fixture has no unreversed transaction available")

        try:
            create_event = await client.post(
                "/source_events",
                headers={"Prefer": "return=minimal"},
                json={
                    "id": str(event_id),
                    "organization_id": str(ORGANIZATION_ID),
                    "provider": "telegram",
                    "external_event_id": f"component-linked-correction-{event_id}",
                    "event_type": "message",
                    "status": "processing",
                    "payload": {
                        "message": {
                            "message_id": event_id.int % 1_000_000,
                            "from": {"id": telegram_user_id},
                            "chat": {"id": component_chat_id},
                            "text": "component correction",
                        }
                    },
                },
            )
            create_event.raise_for_status()

            reversals = SupabaseReversalRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            )
            reversal_request_id = await reversals.begin(
                transaction_id=transaction_id,
                actor_id=ACTOR_ID,
                chat_id=component_chat_id,
            )
            assert (
                await reversals.capture_reason(
                    event_id=event_id,
                    actor_id=ACTOR_ID,
                    chat_id=component_chat_id,
                    reason="component correction",
                )
                == reversal_request_id
            )

            for path, payload in [
                (
                    "/transaction_proposals",
                    {
                        "id": str(proposal_id),
                        "organization_id": str(ORGANIZATION_ID),
                        "location_id": location_id,
                        "source_event_id": str(event_id),
                        "created_by": str(ACTOR_ID),
                        "intent": "receive_stock",
                        "status": "pending_confirmation",
                        "idempotency_key": f"component-linked-correction-{proposal_id}",
                    },
                ),
                (
                    "/proposal_lines",
                    {
                        "id": str(proposal_line_id),
                        "organization_id": str(ORGANIZATION_ID),
                        "proposal_id": str(proposal_id),
                        "line_number": 1,
                        "source_text": "corrected component receipt",
                        "requested_quantity": 5,
                        "requested_unit": "each",
                        "item_variant_id": variant_id,
                        "base_quantity_delta": 5,
                        "base_unit": "each",
                        "match_method": "exact_identifier",
                    },
                ),
            ]:
                response = await client.post(
                    path,
                    headers={"Prefer": "return=minimal"},
                    json=payload,
                )
                response.raise_for_status()

            assert (
                await reversals.attach_replacement(
                    request_id=reversal_request_id,
                    proposal_id=proposal_id,
                    actor_id=ACTOR_ID,
                )
                == proposal_id
            )
            linked = await client.get(
                "/transaction_reversal_requests",
                params={
                    "select": "replacement_proposal_id",
                    "id": f"eq.{reversal_request_id}",
                },
            )
            linked.raise_for_status()
            assert linked.json() == [{"replacement_proposal_id": str(proposal_id)}]

            await reversals.cancel(
                request_id=reversal_request_id,
                actor_id=ACTOR_ID,
            )
            proposal = await client.get(
                "/transaction_proposals",
                params={"select": "status", "id": f"eq.{proposal_id}"},
            )
            proposal.raise_for_status()
            assert proposal.json() == [{"status": "rejected"}]
        finally:
            if reversal_request_id is not None:
                await client.delete(
                    "/transaction_reversal_requests",
                    params={"id": f"eq.{reversal_request_id}"},
                )
            await client.delete(
                "/proposal_lines",
                params={"id": f"eq.{proposal_line_id}"},
            )
            await client.delete(
                "/transaction_proposals",
                params={"id": f"eq.{proposal_id}"},
            )
            await client.delete(
                "/source_events",
                params={"id": f"eq.{event_id}"},
            )


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
            assert sender.messages == [
                (
                    100000001,
                    "❓ **More information needed**\nWhich item should I use?",
                )
            ]

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

            result = await processor.process(event_id)

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


async def test_agent_text_processing_crosses_python_and_local_supabase_boundaries() -> None:
    settings, secret_key = local_supabase()
    event_id = uuid4()
    cancel_event_id = uuid4()
    component_chat_id = -(3_000_000_000 + event_id.int % 1_000_000_000)
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
                "external_event_id": f"component-agent-{event_id}",
                "event_type": "message",
                "payload": {
                    "update_id": 88002,
                    "message": {
                        "message_id": 89,
                        "from": {"id": telegram_user_id},
                        "chat": {"id": component_chat_id},
                        "text": "received three AMOX-500 expiring 30 June 2027",
                    },
                },
            },
        )
        create_event.raise_for_status()
        try:
            events = SupabaseSourceEventWorkRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            )
            agent_repository = SupabaseAgentRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            )
            candidate_repository = SupabaseInventoryCandidateRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            )
            model = ExactReceiptAgentModel()
            processor = TelegramAgentTextEventProcessor(
                events=events,
                model=model,
                conversations=agent_repository,
                catalog_reader=GroundedAgentCatalogReader(
                    candidates=candidate_repository,
                    semantic=None,
                    reads=agent_repository,
                    strategy=MatchingStrategy.FUZZY,
                ),
                reads=agent_repository,
                proposals=SupabaseProposalRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                proposal_actions=SupabaseProposalActionRepository(
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
                catalog_interpreter=UnusedCatalogDetailsInterpreter(),  # type: ignore[arg-type]
            )

            result = await processor.process(event_id)

            assert result.status is TextEventProcessingStatus.PROPOSAL_READY
            assert result.proposal_id is not None
            assert model.calls == 3

            proposal = await client.get(
                "/transaction_proposals",
                params={
                    "select": (
                        "status,intent,notes,proposal_lines("
                        "item_variant_id,base_quantity_delta,attributes)"
                    ),
                    "id": f"eq.{result.proposal_id}",
                },
            )
            proposal.raise_for_status()
            stored_proposal = proposal.json()[0]
            assert stored_proposal["status"] == "pending_confirmation"
            assert stored_proposal["intent"] == "receive_stock"
            assert stored_proposal["notes"] == "Telegram delivery"
            assert stored_proposal["proposal_lines"] == [
                {
                    "item_variant_id": "21000000-0000-0000-0000-000000000003",
                    "base_quantity_delta": 3,
                    "attributes": {"expiry_date": "2027-06-30"},
                }
            ]

            conversation = await client.get(
                "/inventory_agent_conversations",
                params={
                    "select": (
                        "history,allowed_variant_ids,last_source_event_id,"
                        "last_reply_text,last_proposal_id"
                    ),
                    "chat_id": f"eq.{component_chat_id}",
                },
            )
            conversation.raise_for_status()
            stored_conversation = conversation.json()[0]
            assert len(stored_conversation["history"]) == 6
            assert (
                "21000000-0000-0000-0000-000000000003" in stored_conversation["allowed_variant_ids"]
            )
            assert stored_conversation["last_source_event_id"] == str(event_id)
            assert stored_conversation["last_reply_text"] == (
                "I prepared the receipt. Please confirm it."
            )
            assert stored_conversation["last_proposal_id"] == str(result.proposal_id)

            outbox = await client.get(
                "/processing_outbox",
                params={
                    "select": "status,outcome_type,aggregate_id,payload",
                    "source_event_id": f"eq.{event_id}",
                },
            )
            outbox.raise_for_status()
            assert outbox.json() == [
                {
                    "status": "pending",
                    "outcome_type": "proposal_ready",
                    "aggregate_id": str(result.proposal_id),
                    "payload": {
                        "proposal_id": str(result.proposal_id),
                        "agent_reply": "I prepared the receipt. Please confirm it.",
                    },
                }
            ]

            create_cancel_event = await client.post(
                "/source_events",
                headers={"Prefer": "return=minimal"},
                json={
                    "id": str(cancel_event_id),
                    "organization_id": str(ORGANIZATION_ID),
                    "provider": "telegram",
                    "external_event_id": f"component-agent-cancel-{cancel_event_id}",
                    "event_type": "message",
                    "payload": {
                        "update_id": 88003,
                        "message": {
                            "message_id": 90,
                            "from": {"id": telegram_user_id},
                            "chat": {"id": component_chat_id},
                            "text": "Cancel",
                        },
                    },
                },
            )
            create_cancel_event.raise_for_status()

            cancel_result = await processor.process(cancel_event_id)

            assert cancel_result.status is TextEventProcessingStatus.AGENT_MESSAGE
            assert cancel_result.proposal_id == result.proposal_id
            assert model.calls == 3
            cancelled_proposal = await client.get(
                "/transaction_proposals",
                params={
                    "select": "status",
                    "id": f"eq.{result.proposal_id}",
                },
            )
            cancelled_proposal.raise_for_status()
            assert cancelled_proposal.json() == [{"status": "rejected"}]
            cancelled_conversation = await client.get(
                "/inventory_agent_conversations",
                params={
                    "select": "last_source_event_id,last_proposal_id,model_name",
                    "chat_id": f"eq.{component_chat_id}",
                },
            )
            cancelled_conversation.raise_for_status()
            assert cancelled_conversation.json() == [
                {
                    "last_source_event_id": str(cancel_event_id),
                    "last_proposal_id": None,
                    "model_name": "deterministic-proposal-control",
                }
            ]
        finally:
            delete_conversation = await client.delete(
                "/inventory_agent_conversations",
                params={"chat_id": f"eq.{component_chat_id}"},
            )
            delete_conversation.raise_for_status()
            delete_proposal = await client.delete(
                "/transaction_proposals",
                params={"source_event_id": f"eq.{event_id}"},
            )
            delete_proposal.raise_for_status()
            cleanup = await client.delete(
                "/source_events",
                params={"id": f"in.({event_id},{cancel_event_id})"},
            )
            cleanup.raise_for_status()


async def test_match_clarification_resumes_a_real_persisted_proposal() -> None:
    settings, secret_key = local_supabase()
    proposal_event_id = uuid4()
    reply_event_id = uuid4()
    component_chat_id = -(2_000_000_000 + reply_event_id.int % 1_000_000_000)
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
    async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
        telegram_user_id = await active_telegram_user_id(client)
        for event_id, external_id, text, status in (
            (
                proposal_event_id,
                f"component-clarification-proposal-{proposal_event_id}",
                "received four classic t-shirts",
                "processed",
            ),
            (
                reply_event_id,
                f"component-clarification-reply-{reply_event_id}",
                "It is red and medium.",
                "received",
            ),
        ):
            created = await client.post(
                "/source_events",
                headers={"Prefer": "return=minimal"},
                json={
                    "id": str(event_id),
                    "organization_id": str(ORGANIZATION_ID),
                    "provider": "telegram",
                    "external_event_id": external_id,
                    "event_type": "message",
                    "status": status,
                    "processed_at": (
                        datetime.now(UTC).isoformat() if status == "processed" else None
                    ),
                    "payload": {
                        "message": {
                            "from": {"id": telegram_user_id},
                            "chat": {"id": component_chat_id},
                            "text": text,
                        }
                    },
                },
            )
            created.raise_for_status()

        proposal_id: UUID | None = None
        try:
            created_proposal = await client.post(
                "/rpc/create_inventory_proposal",
                json={
                    "p_organization_id": str(ORGANIZATION_ID),
                    "p_location_id": "12000000-0000-0000-0000-000000000001",
                    "p_source_event_id": str(proposal_event_id),
                    "p_created_by": "11000000-0000-0000-0000-000000000001",
                    "p_intent": "receive_stock",
                    "p_idempotency_key": f"component-clarification-{proposal_event_id}",
                    "p_raw_command": {
                        "schema_version": "1.0",
                        "intent": InventoryIntent.RECEIVE_STOCK,
                        "location_hint": None,
                        "lines": [
                            {
                                "source_text": "four classic t-shirts",
                                "item_reference": {
                                    "type": "NAME",
                                    "value": "classic t-shirt",
                                },
                                "description": "classic t-shirt",
                                "quantity": "4",
                                "unit": "each",
                                "attributes": [],
                            }
                        ],
                        "notes": None,
                        "needs_clarification": False,
                        "clarification_question": None,
                    },
                    "p_model_name": "component-fake-model",
                    "p_model_response_id": "component-clarification-response",
                    "p_prompt_version": "inventory-command-v1",
                    "p_notes": None,
                    "p_lines": [
                        {
                            "line_number": 1,
                            "source_text": "four classic t-shirts",
                            "extracted_description": "classic t-shirt",
                            "requested_quantity": 4,
                            "requested_unit": "each",
                            "match_evidence": {
                                "decision": "clarification_required",
                                "reason": "Colour and size are missing.",
                                "clarification_question": "Which colour and size is it?",
                                "candidates": [
                                    {
                                        "item_variant_id": ("21000000-0000-0000-0000-000000000004"),
                                        "item_id": ("20000000-0000-0000-0000-000000000004"),
                                        "item_name": "Classic T-Shirt",
                                        "variant_name": "Classic T-Shirt - Red / M",
                                        "sku": "SHIRT-RED-M",
                                        "base_unit": "each",
                                        "tracking_mode": "simple",
                                        "match_method": "semantic_rerank",
                                        "match_score": "0.88",
                                        "match_evidence": {
                                            "variant_attributes": {
                                                "colour": "red",
                                                "size": "M",
                                            }
                                        },
                                    }
                                ],
                            },
                            "attributes": {},
                        }
                    ],
                },
            )
            created_proposal.raise_for_status()
            proposal_id = UUID(created_proposal.json())
            clarifications = SupabaseMatchClarificationRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            )
            assert (
                await clarifications.begin(
                    proposal_id=proposal_id,
                    actor_id=UUID("11000000-0000-0000-0000-000000000001"),
                    chat_id=component_chat_id,
                )
                == 1
            )

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
                clarifications=clarifications,
                candidate_judge=SelectRedVariantJudge(),  # type: ignore[arg-type]
            )

            result = await processor.process(reply_event_id)

            assert result.status is TextEventProcessingStatus.PROPOSAL_READY
            stored = await client.get(
                "/transaction_proposals",
                params={
                    "select": (
                        "proposal_lines(item_variant_id,base_quantity_delta,attributes,"
                        "match_evidence)"
                    ),
                    "id": f"eq.{proposal_id}",
                },
            )
            stored.raise_for_status()
            line = stored.json()[0]["proposal_lines"][0]
            assert line["item_variant_id"] == ("21000000-0000-0000-0000-000000000004")
            assert line["base_quantity_delta"] == 4
            assert line["attributes"] == {"colour": "red", "size": "M"}
            assert line["match_evidence"]["selected_after_clarification"] is True
        finally:
            if proposal_id is not None:
                deleted = await client.delete(
                    "/transaction_proposals",
                    params={"id": f"eq.{proposal_id}"},
                )
                deleted.raise_for_status()
            cleanup = await client.delete(
                "/source_events",
                params={"id": f"in.({proposal_event_id},{reply_event_id})"},
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
    component_chat_id = 800_000_000 + callback_event_id.int % 100_000_000
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
        conversation_id: UUID | None = None
        try:
            agent_repository = SupabaseAgentRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            )
            conversation = await agent_repository.load(
                organization_id=ORGANIZATION_ID,
                organization_user_id=ACTOR_ID,
                chat_id=component_chat_id,
            )
            conversation_id = conversation.conversation_id
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
                                "chat": {"id": component_chat_id},
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
                conversation_recorder=agent_repository,
            )
            result = await processor.process_next()

            assert result is not None
            assert result.outcome.action is CallbackAction.CANCEL_PROPOSAL
            assert telegram.answers == [f"query-{callback_event_id}"]
            assert telegram.removed_keyboards == [(component_chat_id, 77)]

            updated_conversation = await agent_repository.load(
                organization_id=ORGANIZATION_ID,
                organization_user_id=ACTOR_ID,
                chat_id=component_chat_id,
            )
            callback_items = [
                item for item in updated_conversation.history if item.get("role") == "system"
            ]
            assert callback_items == [
                {
                    "role": "system",
                    "content": (
                        "Inventory system event: The user cancelled stock proposal "
                        f"{proposal_id}. It was not applied and no inventory change "
                        "resulted from that proposal."
                    ),
                }
            ]
            assert updated_conversation.active_turns[-1].source_event_id == callback_event_id

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
            assert telegram.messages == [
                (
                    component_chat_id,
                    "🚫 **Proposal cancelled**",
                )
            ]

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
            if conversation_id is not None:
                delete_conversation = await client.delete(
                    "/inventory_agent_conversations",
                    params={"id": f"eq.{conversation_id}"},
                )
                delete_conversation.raise_for_status()
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


async def test_ambiguous_invoice_reply_resumes_preserved_lines_in_local_supabase() -> None:
    settings, secret_key = local_supabase()
    image_event_id = uuid4()
    reply_event_id = uuid4()
    chat_id = -(2_000_000_000 + image_event_id.int % 1_000_000_000)
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
    proposal_id: UUID | None = None
    async with httpx.AsyncClient(base_url=rest_url, headers=headers) as client:
        member = await client.get(
            "/organization_users",
            params={
                "select": "id,telegram_user_id",
                "organization_id": f"eq.{ORGANIZATION_ID}",
                "id": f"eq.{ACTOR_ID}",
                "active": "eq.true",
                "limit": "1",
            },
        )
        member.raise_for_status()
        telegram_user_id = int(member.json()[0]["telegram_user_id"])
        location = await client.get(
            "/locations",
            params={
                "select": "id",
                "organization_id": f"eq.{ORGANIZATION_ID}",
                "active": "eq.true",
                "limit": "1",
            },
        )
        location.raise_for_status()
        location_id = UUID(location.json()[0]["id"])
        for event_id, event_type in (
            (image_event_id, "invoice_image"),
            (reply_event_id, "message"),
        ):
            created = await client.post(
                "/source_events",
                headers={"Prefer": "return=minimal"},
                json={
                    "id": str(event_id),
                    "organization_id": str(ORGANIZATION_ID),
                    "provider": "component_test",
                    "external_event_id": f"component-command-clarification-{event_id}",
                    "event_type": event_type,
                    "status": "processing",
                    "payload": {},
                },
            )
            created.raise_for_status()

        command_clarifications = SupabaseCommandClarificationRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        handler = InventoryCommandHandler(
            matcher=NoMatchComponentMatcher(),
            proposals=SupabaseProposalRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            ),
            outbox=SupabaseProcessingOutboxRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            ),
            command_clarifications=command_clarifications,
        )
        original = CommandExtractionResult(
            command=ExtractedInventoryCommand.model_validate(
                {
                    "schema_version": "1.0",
                    "intent": "UNKNOWN",
                    "location_hint": None,
                    "lines": [
                        {
                            "source_text": "INV-WIDGET-91 7 boxes",
                            "item_reference": {
                                "type": "PART_NUMBER",
                                "value": "INV-WIDGET-91",
                            },
                            "description": "Invoice Widget",
                            "quantity": "7",
                            "unit": "box",
                            "attributes": [],
                        }
                    ],
                    "notes": "component invoice",
                    "needs_clarification": True,
                    "clarification_question": (
                        "Should these invoice lines be recorded as received stock?"
                    ),
                }
            ),
            response_id="component-image-clarification",
            model="component-fake-model",
            prompt_version="inventory-invoice-image-v1",
        )
        image_context = TelegramTextEventContext(
            event_id=image_event_id,
            organization_id=ORGANIZATION_ID,
            organization_user_id=ACTOR_ID,
            location_id=location_id,
            external_event_id=f"component-image-{image_event_id}",
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            message_text="",
        )
        try:
            clarification_result = await handler.handle(
                context=image_context,
                extraction=original,
            )
            assert clarification_result.status is TextEventProcessingStatus.CLARIFICATION_REQUIRED
            request_id = await command_clarifications.find_pending(
                actor_id=ACTOR_ID,
                chat_id=chat_id,
            )
            assert request_id is not None
            preserved = await command_clarifications.get_view(request_id=request_id)
            assert preserved.extraction.command.lines[0].item_reference.value == "INV-WIDGET-91"
            assert preserved.extraction.command.lines[0].quantity == "7"

            resolved = CommandExtractionResult(
                command=original.command.model_copy(
                    update={
                        "intent": InventoryIntent.RECEIVE_STOCK,
                        "needs_clarification": False,
                        "clarification_question": None,
                    }
                ),
                response_id="component-command-resolved",
                model="component-fake-model",
                prompt_version="inventory-command-clarification-v1",
            )
            reply_context = image_context.model_copy(
                update={
                    "event_id": reply_event_id,
                    "external_event_id": f"component-reply-{reply_event_id}",
                    "message_text": "Yes, all received stock.",
                }
            )
            proposal_result = await handler.handle(
                context=reply_context,
                extraction=resolved,
            )
            proposal_id = proposal_result.proposal_id
            assert proposal_id is not None
            await command_clarifications.resolve(
                request_id=request_id,
                event_id=reply_event_id,
                actor_id=ACTOR_ID,
                user_reply="Yes, all received stock.",
                extraction=resolved,
                proposal_id=proposal_id,
            )
            proposal = await client.get(
                "/transaction_proposals",
                params={
                    "select": "intent,raw_command",
                    "id": f"eq.{proposal_id}",
                },
            )
            proposal.raise_for_status()
            assert proposal.json()[0]["intent"] == "receive_stock"
            assert proposal.json()[0]["raw_command"]["lines"][0]["quantity"] == "7"
            assert (
                await command_clarifications.find_pending(
                    actor_id=ACTOR_ID,
                    chat_id=chat_id,
                )
                is None
            )
        finally:
            if proposal_id is not None:
                delete_proposal = await client.delete(
                    "/transaction_proposals",
                    params={"id": f"eq.{proposal_id}"},
                )
                delete_proposal.raise_for_status()
            for event_id in (reply_event_id, image_event_id):
                cleanup = await client.delete(
                    "/source_events",
                    params={"id": f"eq.{event_id}"},
                )
                cleanup.raise_for_status()
