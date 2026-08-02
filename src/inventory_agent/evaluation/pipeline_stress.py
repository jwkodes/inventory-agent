"""Seeded, no-credit end-to-end stress evaluation for the inventory pipeline.

The runner uses local Supabase and the production application boundaries, while replacing
OpenAI and Telegram network calls with deterministic simulators. It deliberately retains
the generated organization so operators can inspect the complete audit trail afterward.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr

from inventory_agent.agent.production_tools import GroundedAgentCatalogReader
from inventory_agent.agent.repository import SupabaseAgentRepository
from inventory_agent.agent.runtime import FunctionCall, ModelTurn
from inventory_agent.catalog.batch import CatalogBatchExtractionResult
from inventory_agent.catalog.interpreter import CatalogDetailsExtractionResult
from inventory_agent.catalog.models import (
    CatalogBatchCreationView,
    CatalogItemCreationView,
    CatalogTrackingMode,
    ExtractedCatalogBatchDetails,
    ExtractedCatalogBatchItemDetails,
    ExtractedCatalogItemDetails,
)
from inventory_agent.catalog.repository import SupabaseCatalogItemCreationRepository
from inventory_agent.config import Settings, get_settings
from inventory_agent.main import create_app
from inventory_agent.matching.repository import SupabaseInventoryCandidateRepository
from inventory_agent.matching.service import MatchingStrategy
from inventory_agent.processing.agent_text_events import TelegramAgentTextEventProcessor
from inventory_agent.processing.callback_events import (
    CallbackEventProcessingResult,
    TelegramCallbackEventProcessor,
)
from inventory_agent.processing.delivery import TelegramOutboxDeliveryWorker
from inventory_agent.processing.models import (
    OutboxDeliveryStatus,
    TelegramCallbackEventContext,
    TextEventProcessingResult,
)
from inventory_agent.processing.repository import (
    SupabaseProcessingOutboxDeliveryRepository,
    SupabaseProcessingOutboxRepository,
    SupabaseSourceEventWorkRepository,
)
from inventory_agent.proposals.actions import SupabaseProposalActionRepository
from inventory_agent.proposals.repository import SupabaseProposalRepository
from inventory_agent.reversals.repository import SupabaseReversalRepository
from inventory_agent.telegram.callback_dispatcher import TelegramCallbackDispatcher
from inventory_agent.telegram.callbacks import CallbackAction, decode_callback


class ScenarioKind(StrEnum):
    ADD = "add"
    DEDUCT = "deduct"
    CANCEL = "cancel"
    READ = "read"
    REVERSAL = "reversal"
    NEW_SINGLE = "new_single"
    NEW_BATCH = "new_batch"
    NEW_BATCH_DUPLICATE_SKU = "new_batch_duplicate_sku"
    NEW_BATCH_EXISTING_SKU = "new_batch_existing_sku"
    TRANSACTION_BY_TIME = "transaction_by_time"
    TRANSACTION_BY_PRODUCT = "transaction_by_product"
    TRANSACTION_BY_ACTOR = "transaction_by_actor"
    TRANSACTION_N_AGO = "transaction_n_ago"
    UNSAFE_UNGROUNDED = "unsafe_ungrounded"
    UNSAFE_NEGATIVE = "unsafe_negative"
    WRONG_OPERATION = "wrong_operation"


class AssistantStyle(StrEnum):
    CLEAR = "clear"
    VERBOSE = "verbose"
    PREMATURE_SUCCESS = "premature_success"
    BROKEN_MARKDOWN = "broken_markdown"


@dataclass(frozen=True, slots=True)
class TestMember:
    member_id: UUID
    telegram_user_id: int
    display_name: str
    role: str


@dataclass(frozen=True, slots=True)
class TestVariant:
    item_id: UUID
    variant_id: UUID
    item_name: str
    variant_name: str | None
    sku: str
    attributes: dict[str, str]


@dataclass(slots=True)
class ScenarioPlan:
    number: int
    kind: ScenarioKind
    actor: TestMember
    confirmer: TestMember
    target: TestVariant
    quantity: Decimal
    user_text: str
    chat_id: int
    group_chat: bool
    typed_control: bool
    duplicate_update: bool
    assistant_style: AssistantStyle
    new_items: list[dict[str, object]] = field(default_factory=list)
    transaction_id: UUID | None = None
    transaction_query: str | None = None
    transaction_actor: TestMember | None = None
    before_quantity: Decimal = Decimal("0")

    @property
    def name(self) -> str:
        return f"{self.number:03d}-{self.kind.value}"


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int
    chat_id: int
    text: str
    keyboard: list[list[dict[str, str]]] | None


@dataclass(slots=True)
class ScenarioResult:
    name: str
    kind: str
    user_text: str
    passed: bool
    safety_passed: bool
    correctness_passed: bool
    ux_passed: bool
    injected_model_fault: str | None
    issues: list[str]
    timings_ms: dict[str, float]
    transaction_ids: list[str]
    messages: list[str]


@dataclass(frozen=True, slots=True)
class StressRunReport:
    seed: int
    started_at: datetime
    finished_at: datetime
    organization_id: UUID
    organization_name: str
    organization_slug: str
    requested_scenarios: int
    simulated_users: int
    results: list[ScenarioResult]


class RecordingTelegram:
    """Record exact Telegram output while providing callback acknowledgements."""

    def __init__(self) -> None:
        self.messages: list[SentMessage] = []
        self.callback_answers: list[str] = []
        self.removed_keyboards: list[tuple[int, int]] = []
        self._next_message_id = 50_000

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        self._next_message_id += 1
        self.messages.append(
            SentMessage(
                message_id=self._next_message_id,
                chat_id=chat_id,
                text=text,
                keyboard=inline_keyboard,
            )
        )
        return self._next_message_id

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        del text, show_alert
        self.callback_answers.append(callback_query_id)

    async def remove_inline_keyboard(self, *, chat_id: int, message_id: int) -> None:
        self.removed_keyboards.append((chat_id, message_id))


class ScenarioCatalogInterpreter:
    """Simulate structured catalog extraction without an OpenAI request."""

    def __init__(self) -> None:
        self.plan: ScenarioPlan | None = None

    def prepare(self, plan: ScenarioPlan) -> None:
        self.plan = plan

    async def interpret(
        self,
        *,
        user_text: str,
        view: CatalogItemCreationView,
    ) -> CatalogDetailsExtractionResult:
        del user_text
        plan = _required_plan(self.plan)
        sku = plan.new_items[0]["sku"] if plan.new_items else None
        return CatalogDetailsExtractionResult(
            details=ExtractedCatalogItemDetails(
                applies_to_pending_request=True,
                name=None,
                sku=str(sku) if sku else None,
                base_unit=None,
                tracking_mode=None,
                attributes=[],
            ),
            response_id=f"stress-catalog-{plan.number}-{view.request_id}",
            model="stress-catalog-simulator",
        )


class ScenarioBatchInterpreter:
    """Simulate one natural reply that supplies all missing bulk SKUs."""

    def __init__(self) -> None:
        self.plan: ScenarioPlan | None = None

    def prepare(self, plan: ScenarioPlan) -> None:
        self.plan = plan

    async def interpret(
        self,
        *,
        user_text: str,
        view: CatalogBatchCreationView,
    ) -> CatalogBatchExtractionResult:
        del user_text
        plan = _required_plan(self.plan)
        by_line = {index: item for index, item in enumerate(plan.new_items, start=1)}
        return CatalogBatchExtractionResult(
            details=ExtractedCatalogBatchDetails(
                applies_to_pending_request=True,
                items=[
                    ExtractedCatalogBatchItemDetails(
                        line_number=item.line_number,
                        name=None,
                        sku=str(by_line[item.line_number]["sku"]),
                        base_unit=None,
                        tracking_mode=CatalogTrackingMode.SIMPLE,
                        attributes=[],
                    )
                    for item in view.items
                ],
            ),
            response_id=f"stress-batch-{plan.number}-{view.batch_id}",
            model="stress-batch-simulator",
        )


class ScenarioAgentModel:
    """Produce randomized, labelled model turns for one scenario at a time."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._plan: ScenarioPlan | None = None
        self._round = 0

    def prepare(self, plan: ScenarioPlan) -> None:
        self._plan = plan
        self._round = 0

    async def respond(
        self,
        *,
        input_items: list[dict[str, object]],
        instructions: str,
        tools: list[dict[str, object]],
        prompt_cache_key: str | None = None,
        prompt_cache_prefix_item_count: int | None = None,
    ) -> ModelTurn:
        del instructions, tools, prompt_cache_key, prompt_cache_prefix_item_count
        plan = _required_plan(self._plan)
        self._round += 1
        await asyncio.sleep(self._rng.uniform(0.0002, 0.003))

        if plan.kind is ScenarioKind.READ:
            if self._round == 1:
                return _tool_turn(plan, self._round, self._inventory_read(plan))
            output = _latest_tool_output(input_items)
            quantity = _first_on_hand(output)
            return _text_turn(
                plan,
                self._round,
                f"{plan.target.item_name} ({plan.target.sku}): {quantity} each in stock.",
            )

        if plan.kind in {
            ScenarioKind.NEW_SINGLE,
            ScenarioKind.NEW_BATCH,
            ScenarioKind.NEW_BATCH_DUPLICATE_SKU,
            ScenarioKind.NEW_BATCH_EXISTING_SKU,
        }:
            if self._round == 1:
                return _tool_turn(plan, self._round, self._new_item_proposal(plan))
            return _text_turn(plan, self._round, _proposal_note(plan))

        if plan.kind in {
            ScenarioKind.TRANSACTION_BY_TIME,
            ScenarioKind.TRANSACTION_BY_PRODUCT,
            ScenarioKind.TRANSACTION_BY_ACTOR,
            ScenarioKind.TRANSACTION_N_AGO,
        }:
            if self._round == 1:
                return _tool_turn(
                    plan,
                    self._round,
                    FunctionCall(
                        call_id=f"stress-{plan.number}-transaction-read",
                        name="read_transactions",
                        arguments={"query": plan.transaction_query, "limit": 20},
                    ),
                )
            output = _latest_tool_output(input_items)
            transactions = output.get("transactions")
            if plan.kind is ScenarioKind.TRANSACTION_BY_ACTOR:
                has_actor_data = isinstance(transactions, list) and any(
                    isinstance(transaction, dict)
                    and any(
                        field in transaction
                        for field in (
                            "created_by",
                            "created_by_name",
                            "confirmed_by",
                            "confirmed_by_name",
                        )
                    )
                    for transaction in transactions
                )
                if not has_actor_data:
                    return _text_turn(
                        plan,
                        self._round,
                        "The transaction results do not include creator or confirmer identity.",
                    )
            target = (
                next(
                    (
                        transaction
                        for transaction in transactions
                        if isinstance(transaction, dict)
                        and transaction.get("transaction_id") == str(plan.transaction_id)
                    ),
                    None,
                )
                if isinstance(transactions, list)
                else None
            )
            if target is None:
                return _text_turn(
                    plan,
                    self._round,
                    (
                        "I could not identify the requested transaction from the "
                        "authoritative results."
                    ),
                )
            return _text_turn(
                plan,
                self._round,
                (
                    f"Transaction {target['transaction_id']} — "
                    f"{target['transaction_type']} at {target['occurred_at']}: "
                    f"{target['summary']}"
                ),
            )

        if plan.kind is ScenarioKind.UNSAFE_UNGROUNDED:
            if self._round == 1:
                return _tool_turn(
                    plan,
                    self._round,
                    FunctionCall(
                        call_id=f"stress-{plan.number}-unsafe",
                        name="propose_add_inventory",
                        arguments={
                            "lines": [
                                {
                                    "variant_id": str(uuid4()),
                                    "new_item": None,
                                    "quantity": str(plan.quantity),
                                    "unit": "each",
                                    "attributes": [],
                                }
                            ],
                            "reason": "Injected ungrounded model output",
                        },
                    ),
                )
            return _text_turn(
                plan,
                self._round,
                "I could not safely prepare that change because the item was not grounded.",
            )

        if plan.kind is ScenarioKind.UNSAFE_NEGATIVE:
            if self._round == 1:
                return _tool_turn(
                    plan,
                    self._round,
                    FunctionCall(
                        call_id=f"stress-{plan.number}-negative",
                        name="propose_add_inventory",
                        arguments={
                            "lines": [
                                {
                                    "variant_id": str(plan.target.variant_id),
                                    "new_item": None,
                                    "quantity": f"-{plan.quantity}",
                                    "unit": "each",
                                    "attributes": [],
                                }
                            ],
                            "reason": "Injected negative model quantity",
                        },
                    ),
                )
            return _text_turn(
                plan,
                self._round,
                "I could not prepare a negative receipt. Please provide a positive quantity.",
            )

        if plan.kind is ScenarioKind.REVERSAL:
            if self._round == 1:
                assert plan.transaction_id is not None
                return _tool_turn(
                    plan,
                    self._round,
                    FunctionCall(
                        call_id=f"stress-{plan.number}-read-tx",
                        name="read_transactions",
                        arguments={"query": str(plan.transaction_id), "limit": 5},
                    ),
                )
            if self._round == 2:
                transaction_ref = _first_transaction_ref(_latest_tool_output(input_items))
                return _tool_turn(
                    plan,
                    self._round,
                    FunctionCall(
                        call_id=f"stress-{plan.number}-reverse",
                        name="propose_reversal",
                        arguments={
                            "transaction_ref": transaction_ref,
                            "reason": f"Stress correction {plan.number}",
                            "replacement": None,
                        },
                    ),
                )
            return _text_turn(
                plan,
                self._round,
                "I found the exact transaction and prepared its reversal. Please review it.",
            )

        if self._round == 1:
            return _tool_turn(plan, self._round, self._inventory_read(plan))
        if self._round == 2:
            operation = (
                "propose_deduct_inventory"
                if plan.kind in {ScenarioKind.DEDUCT}
                else "propose_add_inventory"
            )
            if plan.kind is ScenarioKind.WRONG_OPERATION:
                operation = "propose_deduct_inventory"
            return _tool_turn(
                plan,
                self._round,
                FunctionCall(
                    call_id=f"stress-{plan.number}-proposal",
                    name=operation,
                    arguments={
                        "lines": [
                            {
                                "variant_id": str(plan.target.variant_id),
                                "new_item": None,
                                "quantity": str(plan.quantity),
                                "unit": "each",
                                "attributes": [],
                            }
                        ],
                        "reason": f"Stress scenario {plan.number}",
                    },
                ),
            )
        return _text_turn(plan, self._round, _proposal_note(plan))

    @staticmethod
    def _inventory_read(plan: ScenarioPlan) -> FunctionCall:
        use_sku = plan.number % 3 != 0
        return FunctionCall(
            call_id=f"stress-{plan.number}-read",
            name="read_inventory",
            arguments={
                "query": None if use_sku else plan.target.item_name,
                "sku": plan.target.sku if use_sku else None,
                "attributes": [],
                "include_zero_stock": True,
                "limit": 10,
            },
        )

    @staticmethod
    def _new_item_proposal(plan: ScenarioPlan) -> FunctionCall:
        lines = [
            {
                "variant_id": None,
                "new_item": {
                    "name": str(item["name"]),
                    "sku": item.get("model_sku"),
                    "base_unit": "each",
                    "tracking_mode": "simple",
                    "attributes": item["attributes"],
                },
                "quantity": str(item["quantity"]),
                "unit": "each",
                "attributes": item["attributes"],
            }
            for item in plan.new_items
        ]
        return FunctionCall(
            call_id=f"stress-{plan.number}-new-items",
            name="propose_add_inventory",
            arguments={"lines": lines, "reason": f"Stress new catalog scenario {plan.number}"},
        )


class SpecificCallbackEventRepository:
    """Claim one known callback so existing real-user retries cannot be consumed."""

    def __init__(
        self,
        *,
        event_id: UUID,
        supabase_url: str,
        secret_key: str,
    ) -> None:
        self._event_id = event_id
        self._rest_url = f"{supabase_url.rstrip('/')}/rest/v1"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._events = SupabaseSourceEventWorkRepository(
            supabase_url=supabase_url,
            secret_key=secret_key,
        )

    async def claim_next_callback_event(self) -> TelegramCallbackEventContext | None:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=30,
        ) as client:
            response = await client.post(
                "/rpc/claim_telegram_callback_event",
                json={"p_event_id": str(self._event_id)},
            )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return None
        return TelegramCallbackEventContext.model_validate(rows[0])

    async def finish_event(
        self,
        *,
        event_id: UUID,
        success: bool,
        error_message: str | None = None,
    ) -> bool:
        return await self._events.finish_event(
            event_id=event_id,
            success=success,
            error_message=error_message,
        )


class PipelineStressRunner:
    """Own one persistent test organization and execute complete scenario workflows."""

    def __init__(
        self,
        *,
        settings: Settings,
        seed: int,
        scenario_count: int,
        user_count: int,
    ) -> None:
        if scenario_count < 101:
            raise ValueError("pipeline stress runs require at least 101 scenarios")
        if user_count < 3:
            raise ValueError("pipeline stress runs require at least 3 simulated users")
        if settings.supabase_url not in {
            "http://127.0.0.1:54321",
            "http://localhost:54321",
        }:
            raise RuntimeError("pipeline stress evaluation is restricted to local Supabase")
        secret = settings.supabase_secret_key
        secret_key = secret.get_secret_value() if secret is not None else ""
        if not secret_key:
            raise RuntimeError("SUPABASE_SECRET_KEY is required")
        self.settings = settings
        self.secret_key = secret_key
        self.seed = seed
        self.scenario_count = scenario_count
        self.user_count = user_count
        self.rng = random.Random(seed)
        self.organization_id = uuid4()
        self.organization_name = f"Inventory Pipeline Stress {seed}"
        self.organization_slug = f"inventory-pipeline-stress-{seed}-{str(self.organization_id)[:8]}"
        self.location_id = uuid4()
        self.members: list[TestMember] = []
        self.variants: list[TestVariant] = []
        self._rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
        self._headers = {
            "apikey": secret_key,
            "Authorization": f"Bearer {secret_key}",
        }
        self._update_id = 8_000_000_000_000 + seed * 10_000
        self.telegram = RecordingTelegram()
        self.model = ScenarioAgentModel(self.rng)
        self.catalog_interpreter = ScenarioCatalogInterpreter()
        self.batch_interpreter = ScenarioBatchInterpreter()

        self.events = SupabaseSourceEventWorkRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        self.agent_repository = SupabaseAgentRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        self.candidates = SupabaseInventoryCandidateRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        self.proposals = SupabaseProposalRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        self.actions = SupabaseProposalActionRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        self.reversals = SupabaseReversalRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        self.catalog = SupabaseCatalogItemCreationRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        self.outbox = SupabaseProcessingOutboxRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        self.delivery_repository = SupabaseProcessingOutboxDeliveryRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        self.delivery = TelegramOutboxDeliveryWorker(
            repository=self.delivery_repository,
            sender=self.telegram,
            display_timezone=settings.inventory_display_timezone,
        )

        from inventory_agent.processing.catalog_batches import CatalogBatchReplyHandler

        self.text_processor = TelegramAgentTextEventProcessor(
            events=self.events,
            model=self.model,
            conversations=self.agent_repository,
            catalog_reader=GroundedAgentCatalogReader(
                candidates=self.candidates,
                semantic=None,
                reads=self.agent_repository,
                strategy=MatchingStrategy.FUZZY,
            ),
            reads=self.agent_repository,
            proposals=self.proposals,
            proposal_actions=self.actions,
            outbox=self.outbox,
            reversals=self.reversals,
            catalog=self.catalog,
            catalog_interpreter=self.catalog_interpreter,
            catalog_batches=CatalogBatchReplyHandler(
                catalog=self.catalog,
                interpreter=self.batch_interpreter,
                outbox=self.outbox,
            ),
            bot_username="stressbot",
        )

        webhook_settings = settings.model_copy(
            update={
                "app_env": "test",
                "telegram_webhook_secret": SecretStr("pipeline-stress-secret"),
                "telegram_bot_username": "stressbot",
                "telegram_bot_token": SecretStr("999999999:pipeline-stress"),
                "inventory_agent_enabled": True,
            }
        )
        self.app = create_app()
        self.app.dependency_overrides[get_settings] = lambda: webhook_settings
        self.webhook = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://pipeline-stress.local",
            headers={"X-Telegram-Bot-Api-Secret-Token": "pipeline-stress-secret"},
            timeout=30,
        )
        self.database = httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=30,
        )

    async def close(self) -> None:
        await self.text_processor.wait_for_background_compactions()
        await self.webhook.aclose()
        await self.database.aclose()

    async def run(self) -> StressRunReport:
        started = datetime.now(UTC)
        await self._seed_organization()
        plans = self._build_plans()
        results: list[ScenarioResult] = []
        for index, plan in enumerate(plans, start=1):
            try:
                result = await self._run_scenario(plan)
            except Exception as error:
                result = ScenarioResult(
                    name=plan.name,
                    kind=plan.kind.value,
                    user_text=plan.user_text,
                    passed=False,
                    safety_passed=False,
                    correctness_passed=False,
                    ux_passed=False,
                    injected_model_fault=_injected_fault(plan),
                    issues=[f"Unhandled {type(error).__name__}: {error}"],
                    timings_ms={},
                    transaction_ids=[],
                    messages=[],
                )
            results.append(result)
            if index % 10 == 0 or index == len(plans):
                passed = sum(value.passed for value in results)
                print(
                    f"stress_progress completed={index}/{len(plans)} "
                    f"passed={passed} failed={len(results) - passed}"
                )
        return StressRunReport(
            seed=self.seed,
            started_at=started,
            finished_at=datetime.now(UTC),
            organization_id=self.organization_id,
            organization_name=self.organization_name,
            organization_slug=self.organization_slug,
            requested_scenarios=self.scenario_count,
            simulated_users=self.user_count,
            results=results,
        )

    async def _seed_organization(self) -> None:
        await self._post(
            "/organizations",
            {
                "id": str(self.organization_id),
                "name": self.organization_name,
                "slug": self.organization_slug,
                "inventory_profile": "stress_test",
                "settings": {
                    "purpose": "persistent end-to-end pipeline stress evidence",
                    "seed": self.seed,
                },
            },
        )
        roles = ("admin", "manager", "worker", "worker", "manager", "worker")
        for index in range(1, self.user_count + 1):
            role = roles[(index - 1) % len(roles)]
            member = TestMember(
                member_id=uuid4(),
                telegram_user_id=1_800_000_000 + (self.seed % 10_000) * 100 + index,
                display_name=f"Stress User {index}",
                role=role,
            )
            self.members.append(member)
        await self._post(
            "/organization_users",
            [
                {
                    "id": str(member.member_id),
                    "organization_id": str(self.organization_id),
                    "telegram_user_id": member.telegram_user_id,
                    "display_name": member.display_name,
                    "role": member.role,
                }
                for member in self.members
            ],
        )
        await self._post(
            "/locations",
            {
                "id": str(self.location_id),
                "organization_id": str(self.organization_id),
                "code": "STRESS",
                "name": "Stress Test Warehouse",
            },
        )
        catalog = [
            ("Industrial Solenoid Valve 2W-10 DC24V N/C", "VALVE-2W10-24-NC", {}),
            ("Industrial Solenoid Valve 2W-10 DC24V N/O", "VALVE-2W10-24-NO", {}),
            ("Nintendo Switch Controller First Edition", "HAC-015", {"edition": "first"}),
            ("Classic T-Shirt", "SHIRT-RED-M", {"colour": "red", "size": "M"}),
            ("Classic T-Shirt", "SHIRT-BLUE-L", {"colour": "blue", "size": "L"}),
            ("Amoxicillin 500mg", "AMOX-500", {"strength": "500mg"}),
            ("Anchor Butter 500g", "BUTTER-500", {}),
            ("Full Cream Milk 1L", "MILK-1L", {}),
        ]
        item_rows: list[dict[str, object]] = []
        variant_rows: list[dict[str, object]] = []
        identifier_rows: list[dict[str, object]] = []
        for item_name, sku, attributes in catalog:
            item_id = uuid4()
            variant_id = uuid4()
            variant_name = _variant_name(item_name, attributes)
            item_rows.append(
                {
                    "id": str(item_id),
                    "organization_id": str(self.organization_id),
                    "name": item_name,
                    "base_unit": "each",
                    "tracking_mode": "simple",
                }
            )
            variant_rows.append(
                {
                    "id": str(variant_id),
                    "organization_id": str(self.organization_id),
                    "item_id": str(item_id),
                    "sku": sku,
                    "name": variant_name,
                    "attributes": attributes,
                }
            )
            identifier_rows.append(
                {
                    "id": str(uuid4()),
                    "organization_id": str(self.organization_id),
                    "item_variant_id": str(variant_id),
                    "identifier_type": (
                        "manufacturer_part_number" if sku in {"HAC-015", "AMOX-500"} else "sku"
                    ),
                    "value": sku,
                    "normalized_value": re.sub(r"[^a-z0-9]", "", sku.casefold()),
                }
            )
            self.variants.append(
                TestVariant(
                    item_id=item_id,
                    variant_id=variant_id,
                    item_name=item_name,
                    variant_name=variant_name,
                    sku=sku,
                    attributes=attributes,
                )
            )
        await self._post("/items", item_rows)
        await self._post("/item_variants", variant_rows)
        await self._post("/item_identifiers", identifier_rows)
        await self._seed_initial_stock()

    async def _seed_initial_stock(self) -> None:
        event_id = uuid4()
        await self._post(
            "/source_events",
            {
                "id": str(event_id),
                "organization_id": str(self.organization_id),
                "provider": "pipeline_stress_seed",
                "external_event_id": f"pipeline-stress-seed-{self.organization_id}",
                "event_type": "seed",
                "status": "processed",
                "processed_at": datetime.now(UTC).isoformat(),
            },
        )
        proposal_response = await self.database.post(
            "/rpc/create_inventory_proposal",
            json={
                "p_organization_id": str(self.organization_id),
                "p_location_id": str(self.location_id),
                "p_source_event_id": str(event_id),
                "p_created_by": str(self.members[0].member_id),
                "p_intent": "receive_stock",
                "p_idempotency_key": f"pipeline-stress-seed-{self.organization_id}",
                "p_raw_command": {"purpose": "stress seed"},
                "p_model_name": "pipeline-stress-seed",
                "p_model_response_id": None,
                "p_prompt_version": None,
                "p_notes": "Seed 500 of each stress-test product",
                "p_lines": [
                    {
                        "line_number": index,
                        "source_text": variant.sku,
                        "extracted_description": variant.item_name,
                        "requested_quantity": 500,
                        "requested_unit": "each",
                        "item_variant_id": str(variant.variant_id),
                        "match_method": "exact_identifier",
                        "match_score": 1,
                        "match_evidence": {"source": "pipeline_stress_seed"},
                        "attributes": {},
                    }
                    for index, variant in enumerate(self.variants, start=1)
                ],
            },
        )
        proposal_response.raise_for_status()
        apply_response = await self.database.post(
            "/rpc/apply_inventory_proposal",
            json={
                "p_proposal_id": proposal_response.json(),
                "p_actor_id": str(self.members[0].member_id),
            },
        )
        apply_response.raise_for_status()

    def _build_plans(self) -> list[ScenarioPlan]:
        required = [
            ScenarioKind.READ,
            ScenarioKind.ADD,
            ScenarioKind.DEDUCT,
            ScenarioKind.CANCEL,
            ScenarioKind.REVERSAL,
            ScenarioKind.NEW_SINGLE,
            ScenarioKind.NEW_BATCH,
            ScenarioKind.NEW_BATCH_DUPLICATE_SKU,
            ScenarioKind.NEW_BATCH_EXISTING_SKU,
            ScenarioKind.TRANSACTION_BY_TIME,
            ScenarioKind.TRANSACTION_BY_PRODUCT,
            ScenarioKind.TRANSACTION_BY_ACTOR,
            ScenarioKind.TRANSACTION_N_AGO,
            ScenarioKind.UNSAFE_UNGROUNDED,
            ScenarioKind.UNSAFE_NEGATIVE,
            ScenarioKind.WRONG_OPERATION,
        ]
        weights = [
            (ScenarioKind.ADD, 28),
            (ScenarioKind.DEDUCT, 20),
            (ScenarioKind.READ, 16),
            (ScenarioKind.CANCEL, 10),
            (ScenarioKind.REVERSAL, 8),
            (ScenarioKind.NEW_SINGLE, 5),
            (ScenarioKind.NEW_BATCH, 4),
            (ScenarioKind.NEW_BATCH_DUPLICATE_SKU, 2),
            (ScenarioKind.NEW_BATCH_EXISTING_SKU, 2),
            (ScenarioKind.TRANSACTION_BY_TIME, 2),
            (ScenarioKind.TRANSACTION_BY_PRODUCT, 2),
            (ScenarioKind.TRANSACTION_BY_ACTOR, 2),
            (ScenarioKind.TRANSACTION_N_AGO, 2),
            (ScenarioKind.UNSAFE_UNGROUNDED, 3),
            (ScenarioKind.UNSAFE_NEGATIVE, 3),
            (ScenarioKind.WRONG_OPERATION, 3),
        ]
        pool = [kind for kind, weight in weights for _ in range(weight)]
        kinds = list(required)
        kinds.extend(self.rng.choice(pool) for _ in range(self.scenario_count - len(kinds)))
        self.rng.shuffle(kinds)
        plans: list[ScenarioPlan] = []
        for number, kind in enumerate(kinds, start=1):
            privileged = [member for member in self.members if member.role in {"admin", "manager"}]
            actor_pool = (
                privileged
                if kind
                in {
                    ScenarioKind.REVERSAL,
                    ScenarioKind.NEW_SINGLE,
                    ScenarioKind.NEW_BATCH,
                    ScenarioKind.NEW_BATCH_DUPLICATE_SKU,
                    ScenarioKind.NEW_BATCH_EXISTING_SKU,
                }
                else self.members
            )
            actor = self.rng.choice(actor_pool)
            confirmer_pool = (
                privileged
                if kind
                in {
                    ScenarioKind.REVERSAL,
                    ScenarioKind.NEW_SINGLE,
                    ScenarioKind.NEW_BATCH,
                    ScenarioKind.NEW_BATCH_DUPLICATE_SKU,
                    ScenarioKind.NEW_BATCH_EXISTING_SKU,
                }
                else self.members
            )
            actor_scoped_workflow = kind in {
                ScenarioKind.REVERSAL,
                ScenarioKind.NEW_SINGLE,
                ScenarioKind.NEW_BATCH,
                ScenarioKind.NEW_BATCH_DUPLICATE_SKU,
                ScenarioKind.NEW_BATCH_EXISTING_SKU,
            }
            confirmer = (
                self.rng.choice(confirmer_pool)
                if not actor_scoped_workflow and self.rng.random() < 0.18
                else actor
            )
            target = self.rng.choice(self.variants)
            quantity = Decimal(self.rng.randint(1, 18))
            group_chat = confirmer != actor or self.rng.random() < 0.25
            chat_id = (
                -(7_000_000_000 + number + self.seed * 1_000)
                if group_chat
                else actor.telegram_user_id
            )
            style = self.rng.choices(
                list(AssistantStyle),
                weights=[89, 5, 4, 2],
                k=1,
            )[0]
            new_items = _new_items_for(kind=kind, number=number, quantity=quantity)
            if kind is ScenarioKind.NEW_BATCH_DUPLICATE_SKU:
                duplicate_sku = f"STRESS-DUPLICATE-{number:03d}"
                for item in new_items:
                    item["model_sku"] = duplicate_sku
            elif kind is ScenarioKind.NEW_BATCH_EXISTING_SKU:
                new_items[0]["model_sku"] = target.sku
            plans.append(
                ScenarioPlan(
                    number=number,
                    kind=kind,
                    actor=actor,
                    confirmer=confirmer,
                    target=target,
                    quantity=quantity,
                    user_text=_random_user_text(
                        rng=self.rng,
                        kind=kind,
                        target=target,
                        quantity=quantity,
                        group_chat=group_chat,
                    ),
                    chat_id=chat_id,
                    group_chat=group_chat,
                    # Standalone Confirm/Cancel is intentionally actor-scoped. Another
                    # member must use the proposal's exact inline button in a group chat.
                    typed_control=self.rng.random() < 0.30 and confirmer == actor,
                    duplicate_update=self.rng.random() < 0.08,
                    assistant_style=style,
                    new_items=new_items,
                )
            )
        return plans

    async def _run_scenario(self, plan: ScenarioPlan) -> ScenarioResult:
        scenario_started = perf_counter()
        timings: dict[str, float] = {}
        issues: list[str] = []
        transactions: list[str] = []
        message_start = len(self.telegram.messages)
        plan.before_quantity = await self._balance(plan.target.variant_id)
        self.model.prepare(plan)
        self.catalog_interpreter.prepare(plan)
        self.batch_interpreter.prepare(plan)

        if plan.kind is ScenarioKind.REVERSAL:
            setup_transaction = await self._prepare_reversal_target(plan, timings)
            transactions.append(str(setup_transaction))
            plan.transaction_id = setup_transaction
            plan.user_text = _reversal_text(self.rng, setup_transaction)
            if plan.group_chat:
                plan.user_text = f"@stressbot {plan.user_text}"
            self.model.prepare(plan)
        elif plan.kind in {
            ScenarioKind.TRANSACTION_BY_TIME,
            ScenarioKind.TRANSACTION_BY_PRODUCT,
            ScenarioKind.TRANSACTION_BY_ACTOR,
            ScenarioKind.TRANSACTION_N_AGO,
        }:
            fixture_transactions = await self._prepare_transaction_read_fixtures(plan)
            transactions.extend(str(value) for value in fixture_transactions)
            plan.before_quantity = await self._balance(plan.target.variant_id)
            message_start = len(self.telegram.messages)
            self.model.prepare(plan)

        ingest_started = perf_counter()
        event_id, ingest_status = await self._ingest_message(
            actor=plan.actor,
            chat_id=plan.chat_id,
            text=plan.user_text,
            group_chat=plan.group_chat,
        )
        timings["webhook_ingest"] = _elapsed_ms(ingest_started)
        if ingest_status != "accepted":
            raise RuntimeError(f"webhook returned {ingest_status}")
        if plan.duplicate_update:
            duplicate_started = perf_counter()
            duplicate_status = await self._repeat_last_update()
            timings["duplicate_webhook"] = _elapsed_ms(duplicate_started)
            if duplicate_status != "duplicate":
                issues.append("Repeated Telegram update was not identified as duplicate")

        process_started = perf_counter()
        result = await self.text_processor.process(event_id)
        timings["agent_processing"] = _elapsed_ms(process_started)
        first_delivery = await self._deliver(result, timings, "initial_delivery")

        if plan.kind is ScenarioKind.READ:
            after = await self._balance(plan.target.variant_id)
            if after != plan.before_quantity:
                issues.append("Read-only request changed inventory")
        elif plan.kind in {
            ScenarioKind.UNSAFE_UNGROUNDED,
            ScenarioKind.UNSAFE_NEGATIVE,
        }:
            after = await self._balance(plan.target.variant_id)
            if after != plan.before_quantity:
                issues.append("Injected unsafe model output changed inventory")
            if result.proposal_id is not None:
                issues.append("Injected unsafe model output created a proposal")
        elif plan.kind is ScenarioKind.NEW_SINGLE:
            transaction_id = await self._finish_new_single(plan, first_delivery, timings)
            transactions.append(str(transaction_id))
        elif plan.kind is ScenarioKind.NEW_BATCH:
            transaction_id = await self._finish_new_batch(plan, first_delivery, timings)
            transactions.append(str(transaction_id))
        elif plan.kind in {
            ScenarioKind.NEW_BATCH_DUPLICATE_SKU,
            ScenarioKind.NEW_BATCH_EXISTING_SKU,
        }:
            transaction_id = await self._finish_conflicting_new_batch(
                plan,
                first_delivery,
                timings,
            )
            transactions.append(str(transaction_id))
        elif plan.kind in {
            ScenarioKind.TRANSACTION_BY_TIME,
            ScenarioKind.TRANSACTION_BY_PRODUCT,
            ScenarioKind.TRANSACTION_BY_ACTOR,
            ScenarioKind.TRANSACTION_N_AGO,
        }:
            after = await self._balance(plan.target.variant_id)
            if after != plan.before_quantity:
                issues.append("Read-only transaction request changed inventory")
            if plan.kind is ScenarioKind.TRANSACTION_BY_ACTOR:
                issues.append(
                    "Unsupported capability: transaction reads do not expose "
                    "creator/confirmer identity"
                )
            elif plan.transaction_id is None or str(plan.transaction_id) not in first_delivery.text:
                issues.append("Transaction retrieval did not identify expected transaction")
        elif plan.kind is ScenarioKind.REVERSAL:
            reversal_transaction = await self._confirm_reversal(plan, first_delivery, timings)
            transactions.append(str(reversal_transaction))
        elif plan.kind is ScenarioKind.CANCEL:
            await self._finish_proposal_control(
                plan,
                first_delivery,
                confirm=False,
                timings=timings,
            )
            after = await self._balance(plan.target.variant_id)
            if after != plan.before_quantity:
                issues.append("Cancelled proposal changed inventory")
        else:
            confirmed_transaction = await self._finish_proposal_control(
                plan,
                first_delivery,
                confirm=True,
                timings=timings,
            )
            if confirmed_transaction is not None:
                transactions.append(str(confirmed_transaction))
            after = await self._balance(plan.target.variant_id)
            expected_operation = (
                -plan.quantity if plan.kind is ScenarioKind.DEDUCT else plan.quantity
            )
            expected = plan.before_quantity + expected_operation
            if after != expected:
                issues.append(f"Expected balance {expected}, observed {after}")
            if (
                plan.kind is ScenarioKind.WRONG_OPERATION
                and after == plan.before_quantity - plan.quantity
            ):
                issues.append(
                    "Model intent inversion was accepted and changed stock in the wrong direction"
                )

        scenario_messages = [message.text for message in self.telegram.messages[message_start:]]
        ux_issues = _judge_ux(plan, scenario_messages)
        issues.extend(ux_issues)
        safety_passed = not any(
            phrase in issue
            for issue in issues
            for phrase in (
                "unsafe model output changed",
                "unsafe model output created",
                "Read-only request changed",
                "Read-only transaction request changed",
                "Cancelled proposal changed",
                "Model intent inversion was accepted",
            )
        )
        correctness_issues = [
            issue
            for issue in issues
            if issue.startswith(
                (
                    "Expected balance",
                    "Repeated Telegram",
                    "Transaction retrieval",
                    "Unsupported capability",
                )
            )
        ]
        correctness_passed = not correctness_issues
        ux_passed = not ux_issues
        injected_fault = _injected_fault(plan)
        passed = safety_passed and correctness_passed and ux_passed
        timings["scenario_total"] = _elapsed_ms(scenario_started)
        return ScenarioResult(
            name=plan.name,
            kind=plan.kind.value,
            user_text=plan.user_text,
            passed=passed,
            safety_passed=safety_passed,
            correctness_passed=correctness_passed,
            ux_passed=ux_passed,
            injected_model_fault=injected_fault,
            issues=issues,
            timings_ms=timings,
            transaction_ids=transactions,
            messages=scenario_messages,
        )

    async def _prepare_transaction_read_fixtures(
        self,
        plan: ScenarioPlan,
    ) -> list[UUID]:
        fixture_count = 6 if plan.kind is ScenarioKind.TRANSACTION_N_AGO else 4
        privileged = [member for member in self.members if member.role in {"admin", "manager"}]
        target_actor = self.rng.choice(
            [member for member in privileged if member.member_id != plan.actor.member_id]
            or privileged
        )
        created: list[UUID] = []
        fixture_variants: list[TestVariant] = []
        for index in range(fixture_count):
            if plan.kind is ScenarioKind.TRANSACTION_BY_PRODUCT and index != fixture_count - 1:
                choices = [
                    variant
                    for variant in self.variants
                    if variant.variant_id != plan.target.variant_id
                ]
                fixture_variants.append(self.rng.choice(choices))
            else:
                fixture_variants.append(plan.target)
        for index, variant in enumerate(fixture_variants, start=1):
            actor = (
                target_actor
                if plan.kind is ScenarioKind.TRANSACTION_BY_ACTOR and index == fixture_count
                else plan.actor
            )
            transaction_id = await self._create_fixture_transaction(
                plan=plan,
                ordinal=index,
                actor=actor,
                variant=variant,
                quantity=Decimal(index),
            )
            created.append(transaction_id)

        if plan.kind is ScenarioKind.TRANSACTION_BY_TIME:
            target_index = 2
            plan.transaction_id = created[target_index - 1]
            records = await self.agent_repository.read_transactions(
                organization_id=self.organization_id,
                query=str(plan.transaction_id),
                limit=1,
            )
            exact = next(
                (record for record in records if record.transaction_id == str(plan.transaction_id)),
                None,
            )
            if exact is None:
                raise RuntimeError("time fixture transaction could not be read by exact ID")
            target_time = datetime.fromisoformat(exact.occurred_at.replace("Z", "+00:00"))
            plan.transaction_query = target_time.strftime("%H:%M")
            plan.user_text = (
                f"Which transaction happened at roughly {target_time.strftime('%H:%M')}?"
            )
        elif plan.kind is ScenarioKind.TRANSACTION_BY_PRODUCT:
            plan.transaction_id = created[-1]
            plan.transaction_query = plan.target.item_name
            plan.user_text = f"Find my latest transaction for roughly {plan.target.item_name}"
        elif plan.kind is ScenarioKind.TRANSACTION_BY_ACTOR:
            plan.transaction_id = created[-1]
            plan.transaction_actor = target_actor
            plan.transaction_query = target_actor.display_name
            plan.user_text = f"Find the latest transaction made by {target_actor.display_name}"
        else:
            n_ago = 4
            plan.transaction_id = created[-n_ago]
            plan.transaction_query = None
            plan.user_text = f"Show me the transaction from {n_ago} transactions ago"
        if plan.group_chat:
            plan.user_text = f"@stressbot {plan.user_text}"
        return created

    async def _create_fixture_transaction(
        self,
        *,
        plan: ScenarioPlan,
        ordinal: int,
        actor: TestMember,
        variant: TestVariant,
        quantity: Decimal,
    ) -> UUID:
        event_id = uuid4()
        await self._post(
            "/source_events",
            {
                "id": str(event_id),
                "organization_id": str(self.organization_id),
                "provider": "pipeline_stress_fixture",
                "external_event_id": (
                    f"pipeline-stress-fixture-{plan.number}-{ordinal}-{event_id}"
                ),
                "event_type": "seed",
                "status": "processed",
                "processed_at": datetime.now(UTC).isoformat(),
            },
        )
        proposal_response = await self.database.post(
            "/rpc/create_inventory_proposal",
            json={
                "p_organization_id": str(self.organization_id),
                "p_location_id": str(self.location_id),
                "p_source_event_id": str(event_id),
                "p_created_by": str(actor.member_id),
                "p_intent": "receive_stock",
                "p_idempotency_key": (
                    f"pipeline-stress-fixture-{plan.number}-{ordinal}-{event_id}"
                ),
                "p_raw_command": {"purpose": "transaction retrieval fixture"},
                "p_model_name": "pipeline-stress-fixture",
                "p_model_response_id": None,
                "p_prompt_version": None,
                "p_notes": "Fixture for authoritative transaction retrieval",
                "p_lines": [
                    {
                        "line_number": 1,
                        "source_text": variant.sku,
                        "extracted_description": variant.item_name,
                        "requested_quantity": str(quantity),
                        "requested_unit": "each",
                        "item_variant_id": str(variant.variant_id),
                        "match_method": "exact_identifier",
                        "match_score": 1,
                        "match_evidence": {"source": "pipeline_stress_fixture"},
                        "attributes": {},
                    }
                ],
            },
        )
        proposal_response.raise_for_status()
        apply_response = await self.database.post(
            "/rpc/apply_inventory_proposal",
            json={
                "p_proposal_id": proposal_response.json(),
                "p_actor_id": str(actor.member_id),
            },
        )
        apply_response.raise_for_status()
        return UUID(str(apply_response.json()))

    async def _prepare_reversal_target(
        self,
        plan: ScenarioPlan,
        timings: dict[str, float],
    ) -> UUID:
        original_kind = plan.kind
        original_text = plan.user_text
        original_style = plan.assistant_style
        plan.kind = ScenarioKind.ADD
        plan.assistant_style = AssistantStyle.CLEAR
        plan.user_text = f"Received {plan.quantity} of {plan.target.sku}"
        if plan.group_chat:
            plan.user_text = f"@stressbot {plan.user_text}"
        try:
            self.model.prepare(plan)
            started = perf_counter()
            event_id, status = await self._ingest_message(
                actor=plan.actor,
                chat_id=plan.chat_id,
                text=plan.user_text,
                group_chat=plan.group_chat,
            )
            if status != "accepted":
                raise RuntimeError("reversal setup webhook was not accepted")
            result = await self.text_processor.process(event_id)
            message = await self._deliver(result, timings, "reversal_setup_delivery")
            transaction_id = await self._finish_proposal_control(
                plan,
                message,
                confirm=True,
                timings=timings,
                timing_prefix="reversal_setup",
            )
            if transaction_id is None:
                raise RuntimeError("reversal setup did not produce a transaction")
            timings["reversal_setup_total"] = _elapsed_ms(started)
            return transaction_id
        finally:
            plan.kind = original_kind
            plan.user_text = original_text
            plan.assistant_style = original_style

    async def _finish_proposal_control(
        self,
        plan: ScenarioPlan,
        message: SentMessage,
        *,
        confirm: bool,
        timings: dict[str, float],
        timing_prefix: str = "proposal",
    ) -> UUID | None:
        if plan.typed_control:
            actor = plan.confirmer if plan.group_chat else plan.actor
            result = await self._send_typed_control(
                plan=plan,
                actor=actor,
                text="Confirm" if confirm else "Cancel",
                timings=timings,
                timing_prefix=timing_prefix,
            )
            delivered = await self._deliver(
                result,
                timings,
                f"{timing_prefix}_control_delivery",
            )
            return _transaction_id_from_message(delivered.text) if confirm else None
        action = CallbackAction.CONFIRM_PROPOSAL if confirm else CallbackAction.CANCEL_PROPOSAL
        callback_result, delivered = await self._press(
            plan=plan,
            message=message,
            action=action,
            actor=plan.confirmer,
            timings=timings,
            timing_prefix=timing_prefix,
        )
        if confirm:
            return callback_result.outcome.result_id
        if "cancel" not in delivered.text.casefold():
            raise RuntimeError("cancel callback did not produce a cancellation notice")
        return None

    async def _finish_new_single(
        self,
        plan: ScenarioPlan,
        initial: SentMessage,
        timings: dict[str, float],
    ) -> UUID:
        _result, catalog_message = await self._press(
            plan=plan,
            message=initial,
            action=CallbackAction.ADD_NEW_ITEM,
            actor=plan.actor,
            timings=timings,
            timing_prefix="new_item_begin",
        )
        if _has_action(catalog_message, CallbackAction.CONFIRM_NEW_ITEM):
            confirmation = catalog_message
        else:
            details_result = await self._send_typed_control(
                plan=plan,
                actor=plan.actor,
                text=f"Use SKU {plan.new_items[0]['sku']}",
                timings=timings,
                timing_prefix="new_item_details",
            )
            confirmation = await self._deliver(
                details_result,
                timings,
                "new_item_details_delivery",
            )
        _created, proposal_message = await self._press(
            plan=plan,
            message=confirmation,
            action=CallbackAction.CONFIRM_NEW_ITEM,
            actor=plan.actor,
            timings=timings,
            timing_prefix="new_item_create",
        )
        transaction_id = await self._finish_proposal_control(
            plan,
            proposal_message,
            confirm=True,
            timings=timings,
            timing_prefix="new_item_stock",
        )
        if transaction_id is None:
            raise RuntimeError("new item receipt did not produce a transaction")
        return transaction_id

    async def _finish_new_batch(
        self,
        plan: ScenarioPlan,
        initial: SentMessage,
        timings: dict[str, float],
    ) -> UUID:
        _result, catalog_message = await self._press(
            plan=plan,
            message=initial,
            action=CallbackAction.ADD_ALL_NEW_ITEMS,
            actor=plan.actor,
            timings=timings,
            timing_prefix="new_batch_begin",
        )
        if _has_action(catalog_message, CallbackAction.CONFIRM_CATALOG_BATCH):
            confirmation = catalog_message
        else:
            details_result = await self._send_typed_control(
                plan=plan,
                actor=plan.actor,
                text="Generate unique internal SKUs from the descriptions",
                timings=timings,
                timing_prefix="new_batch_details",
            )
            confirmation = await self._deliver(
                details_result,
                timings,
                "new_batch_details_delivery",
            )
        callback_result, _applied = await self._press(
            plan=plan,
            message=confirmation,
            action=CallbackAction.CONFIRM_CATALOG_BATCH,
            actor=plan.confirmer,
            timings=timings,
            timing_prefix="new_batch_confirm",
        )
        if callback_result.outcome.result_id is None:
            raise RuntimeError("new batch confirmation did not return a transaction")
        return callback_result.outcome.result_id

    async def _finish_conflicting_new_batch(
        self,
        plan: ScenarioPlan,
        initial: SentMessage,
        timings: dict[str, float],
    ) -> UUID:
        _result, confirmation = await self._press(
            plan=plan,
            message=initial,
            action=CallbackAction.ADD_ALL_NEW_ITEMS,
            actor=plan.actor,
            timings=timings,
            timing_prefix="conflict_batch_begin",
        )
        if _has_action(confirmation, CallbackAction.CONFIRM_CATALOG_BATCH):
            conflict_result, correction_prompt = await self._press(
                plan=plan,
                message=confirmation,
                action=CallbackAction.CONFIRM_CATALOG_BATCH,
                actor=plan.actor,
                timings=timings,
                timing_prefix="conflict_batch_rejected",
            )
            if conflict_result.outcome.catalog_batch_status != "awaiting_details":
                raise RuntimeError("conflicting SKU was not rejected for correction")
            if (
                plan.kind is ScenarioKind.NEW_BATCH_DUPLICATE_SKU
                and "more than once" not in correction_prompt.text.casefold()
            ):
                raise RuntimeError("catalog conflict response did not explain the duplicate SKU")
        else:
            if plan.kind is not ScenarioKind.NEW_BATCH_EXISTING_SKU:
                raise RuntimeError("duplicate SKU batch was not offered for review")
            correction_prompt = confirmation

        details_result = await self._send_typed_control(
            plan=plan,
            actor=plan.actor,
            text="Use these corrected unique internal SKUs for both products",
            timings=timings,
            timing_prefix="conflict_batch_correction",
        )
        corrected_confirmation = await self._deliver(
            details_result,
            timings,
            "conflict_batch_correction_delivery",
        )
        callback_result, _applied = await self._press(
            plan=plan,
            message=corrected_confirmation,
            action=CallbackAction.CONFIRM_CATALOG_BATCH,
            actor=plan.actor,
            timings=timings,
            timing_prefix="conflict_batch_confirm",
        )
        if callback_result.outcome.result_id is None:
            raise RuntimeError("corrected catalog batch did not return a transaction")
        return callback_result.outcome.result_id

    async def _confirm_reversal(
        self,
        plan: ScenarioPlan,
        message: SentMessage,
        timings: dict[str, float],
    ) -> UUID:
        callback_result, _delivered = await self._press(
            plan=plan,
            message=message,
            action=CallbackAction.CONFIRM_REVERSAL,
            actor=plan.confirmer,
            timings=timings,
            timing_prefix="reversal_confirm",
        )
        if callback_result.outcome.result_id is None:
            raise RuntimeError("reversal confirmation did not return a transaction")
        expected = plan.before_quantity
        observed = await self._balance(plan.target.variant_id)
        if observed != expected:
            raise RuntimeError(f"reversal expected balance {expected}, observed {observed}")
        return callback_result.outcome.result_id

    async def _send_typed_control(
        self,
        *,
        plan: ScenarioPlan,
        actor: TestMember,
        text: str,
        timings: dict[str, float],
        timing_prefix: str,
    ) -> TextEventProcessingResult:
        ingest_started = perf_counter()
        event_id, status = await self._ingest_message(
            actor=actor,
            chat_id=plan.chat_id,
            text=f"@stressbot {text}" if plan.group_chat else text,
            group_chat=plan.group_chat,
        )
        timings[f"{timing_prefix}_typed_ingest"] = _elapsed_ms(ingest_started)
        if status != "accepted":
            raise RuntimeError(f"typed control webhook returned {status}")
        process_started = perf_counter()
        result = await self.text_processor.process(event_id)
        timings[f"{timing_prefix}_typed_process"] = _elapsed_ms(process_started)
        return result

    async def _press(
        self,
        *,
        plan: ScenarioPlan,
        message: SentMessage,
        action: CallbackAction,
        actor: TestMember,
        timings: dict[str, float],
        timing_prefix: str,
    ) -> tuple[CallbackEventProcessingResult, SentMessage]:
        callback_data = _callback_for(message, action)
        ingest_started = perf_counter()
        event_id, status = await self._ingest_callback(
            actor=actor,
            chat_id=plan.chat_id,
            message_id=message.message_id,
            callback_data=callback_data,
        )
        timings[f"{timing_prefix}_callback_ingest"] = _elapsed_ms(ingest_started)
        if status != "accepted":
            raise RuntimeError(f"callback webhook returned {status}")
        callback_events = SpecificCallbackEventRepository(
            event_id=event_id,
            supabase_url=self.settings.supabase_url,
            secret_key=self.secret_key,
        )
        processor = TelegramCallbackEventProcessor(
            events=callback_events,
            dispatcher=TelegramCallbackDispatcher(
                answerer=self.telegram,
                repository=self.actions,
                reversals=self.reversals,
                catalog=self.catalog,
            ),
            message_editor=self.telegram,
            outbox=self.outbox,
            conversation_recorder=self.agent_repository,
        )
        process_started = perf_counter()
        result = await processor.process_next()
        timings[f"{timing_prefix}_callback_process"] = _elapsed_ms(process_started)
        if result is None:
            raise RuntimeError("callback was not claimable")
        outbox_id = await self._outbox_id(event_id)
        delivery_started = perf_counter()
        delivery_result = await self.delivery.deliver_one(outbox_id)
        timings[f"{timing_prefix}_callback_delivery"] = _elapsed_ms(delivery_started)
        if delivery_result.status is not OutboxDeliveryStatus.SENT:
            raise RuntimeError(f"callback delivery returned {delivery_result.status}")
        return result, self.telegram.messages[-1]

    async def _deliver(
        self,
        result: TextEventProcessingResult,
        timings: dict[str, float],
        timing_name: str,
    ) -> SentMessage:
        if result.outbox_id is None:
            raise RuntimeError(f"{result.status} did not enqueue a Telegram outcome")
        started = perf_counter()
        delivered = await self.delivery.deliver_one(result.outbox_id)
        timings[timing_name] = _elapsed_ms(started)
        if delivered.status is not OutboxDeliveryStatus.SENT:
            raise RuntimeError(f"outbox delivery returned {delivered.status}")
        return self.telegram.messages[-1]

    async def _ingest_message(
        self,
        *,
        actor: TestMember,
        chat_id: int,
        text: str,
        group_chat: bool,
    ) -> tuple[UUID, str]:
        self._update_id += 1
        self._last_payload = {
            "update_id": self._update_id,
            "message": {
                "message_id": self._update_id,
                "from": {
                    "id": actor.telegram_user_id,
                    "first_name": actor.display_name,
                },
                "chat": {
                    "id": chat_id,
                    "type": "supergroup" if group_chat else "private",
                },
                "text": text,
            },
        }
        response = await self.webhook.post("/webhooks/telegram", json=self._last_payload)
        response.raise_for_status()
        response_body = response.json()
        status = str(response_body["status"])
        if status not in {"accepted", "duplicate"}:
            raise RuntimeError(f"message webhook did not persist event: {response_body}")
        event_id = await self._event_id(self._update_id)
        return event_id, status

    async def _repeat_last_update(self) -> str:
        response = await self.webhook.post("/webhooks/telegram", json=self._last_payload)
        response.raise_for_status()
        return str(response.json()["status"])

    async def _ingest_callback(
        self,
        *,
        actor: TestMember,
        chat_id: int,
        message_id: int,
        callback_data: str,
    ) -> tuple[UUID, str]:
        self._update_id += 1
        payload = {
            "update_id": self._update_id,
            "callback_query": {
                "id": f"stress-query-{self._update_id}",
                "from": {"id": actor.telegram_user_id},
                "data": callback_data,
                "message": {
                    "message_id": message_id,
                    "chat": {"id": chat_id, "type": "supergroup" if chat_id < 0 else "private"},
                },
            },
        }
        response = await self.webhook.post("/webhooks/telegram", json=payload)
        response.raise_for_status()
        response_body = response.json()
        status = str(response_body["status"])
        if status not in {"accepted", "duplicate"}:
            raise RuntimeError(f"callback webhook did not persist event: {response_body}")
        event_id = await self._event_id(self._update_id)
        return event_id, status

    async def _event_id(self, update_id: int) -> UUID:
        response = await self.database.get(
            "/source_events",
            params={
                "select": "id",
                "provider": "eq.telegram",
                "external_event_id": f"eq.{update_id}",
                "limit": "1",
            },
        )
        response.raise_for_status()
        rows = response.json()
        if len(rows) != 1:
            raise RuntimeError(f"Telegram event {update_id} was not persisted")
        return UUID(rows[0]["id"])

    async def _outbox_id(self, event_id: UUID) -> UUID:
        response = await self.database.get(
            "/processing_outbox",
            params={
                "select": "id",
                "source_event_id": f"eq.{event_id}",
                "limit": "1",
            },
        )
        response.raise_for_status()
        rows = response.json()
        if len(rows) != 1:
            raise RuntimeError(f"source event {event_id} has no outbox outcome")
        return UUID(rows[0]["id"])

    async def _balance(self, variant_id: UUID) -> Decimal:
        response = await self.database.get(
            "/inventory_balances",
            params={
                "select": "quantity",
                "organization_id": f"eq.{self.organization_id}",
                "location_id": f"eq.{self.location_id}",
                "item_variant_id": f"eq.{variant_id}",
                "lot_id": "is.null",
                "serial_id": "is.null",
                "limit": "1",
            },
        )
        response.raise_for_status()
        rows = response.json()
        return Decimal(str(rows[0]["quantity"])) if rows else Decimal("0")

    async def _post(self, path: str, body: object) -> None:
        response = await self.database.post(
            path,
            headers={"Prefer": "return=minimal"},
            json=body,
        )
        response.raise_for_status()


def render_report(report: StressRunReport) -> str:
    results = report.results
    passed = sum(result.passed for result in results)
    safety = sum(result.safety_passed for result in results)
    correctness = sum(result.correctness_passed for result in results)
    ux = sum(result.ux_passed for result in results)
    by_kind: dict[str, list[ScenarioResult]] = defaultdict(list)
    for result in results:
        by_kind[result.kind].append(result)
    timing_values: dict[str, list[float]] = defaultdict(list)
    for result in results:
        for stage, duration in result.timings_ms.items():
            timing_values[stage].append(duration)
    issue_counts = Counter(_finding_label(issue) for result in results for issue in result.issues)
    duration = (report.finished_at - report.started_at).total_seconds()
    intent_inversions = sum(
        result.kind == ScenarioKind.WRONG_OPERATION.value and not result.safety_passed
        for result in results
    )
    identity_gaps = sum(
        any(issue.startswith("Unsupported capability") for issue in result.issues)
        for result in results
    )
    unexpected_failures = len(results) - passed - intent_inversions - identity_gaps
    lines = [
        "# Full-pipeline randomized stress report",
        "",
        f"- Generated: {report.finished_at.isoformat()}",
        f"- Seed: `{report.seed}`",
        f"- Scenarios: **{len(results)}**",
        f"- Simulated company members: **{report.simulated_users}**",
        f"- Wall time: **{duration:.2f} seconds**",
        "- OpenAI cost: **$0** (schema-compatible model simulation)",
        f"- Persistent organization: **{report.organization_name}**",
        f"- Organization ID: `{report.organization_id}`",
        f"- Organization slug: `{report.organization_slug}`",
        "",
        "## Executive summary",
        "",
        f"- Overall strict pass: **{passed}/{len(results)} ({_percent(passed, len(results))})**",
        f"- Inventory safety: **{safety}/{len(results)} ({_percent(safety, len(results))})**",
        (
            f"- Database correctness/idempotency: **{correctness}/{len(results)} "
            f"({_percent(correctness, len(results))})**"
        ),
        f"- Telegram UX judge: **{ux}/{len(results)} ({_percent(ux, len(results))})**",
        "",
        (
            f"Strict failures comprise **{intent_inversions}** deliberately injected semantic "
            f"intent inversions, **{identity_gaps}** known transaction-identity capability gaps, "
            f"and **{unexpected_failures}** unexpected failures."
        ),
        "",
        "## Scenario coverage",
        "",
        "| Kind | Runs | Strict pass | Safety pass | Correctness pass | UX pass |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for kind in sorted(by_kind):
        group = by_kind[kind]
        lines.append(
            f"| {kind} | {len(group)} | {sum(value.passed for value in group)} | "
            f"{sum(value.safety_passed for value in group)} | "
            f"{sum(value.correctness_passed for value in group)} | "
            f"{sum(value.ux_passed for value in group)} |"
        )
    lines.extend(
        [
            "",
            "## Latency",
            "",
            "These are local application/database timings with simulated model and Telegram "
            "network boundaries. They expose Python/PostgreSQL bottlenecks but do not estimate "
            "real OpenAI or Telegram internet latency.",
            "",
            "| Stage | Samples | p50 ms | p95 ms | max ms |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for stage, values in sorted(
        timing_values.items(),
        key=lambda item: _percentile(item[1], 95),
        reverse=True,
    ):
        lines.append(
            f"| `{stage}` | {len(values)} | {statistics.median(values):.2f} | "
            f"{_percentile(values, 95):.2f} | {max(values):.2f} |"
        )
    scenario_p95 = _percentile(timing_values.get("scenario_total", []), 95)
    agent_p95 = _percentile(timing_values.get("agent_processing", []), 95)
    reversal_p95 = _percentile(timing_values.get("reversal_setup_total", []), 95)
    lines.extend(
        [
            "",
            "### Bottleneck assessment",
            "",
            (
                f"- A complete simulated journey had a p95 of **{scenario_p95:.2f} ms**. "
                "That includes every local webhook, processor, callback, outbox, and ledger "
                "step needed by the scenario."
            ),
            (
                "- Initial inventory-agent processing was the largest repeated single stage "
                f"at **{agent_p95:.2f} ms p95** even with network/model latency removed."
            ),
            (
                "- Reversal setup was the slowest workflow-specific path at "
                f"**{reversal_p95:.2f} ms p95** because the harness first creates and confirms "
                "a real target transaction before retrieving and reversing it."
            ),
            "- In production, OpenAI and Telegram network latency will be additional. This run "
            "does not claim to measure either external service.",
        ]
    )
    lines.extend(["", "## Findings", ""])
    if issue_counts:
        for issue, count in issue_counts.most_common():
            lines.append(f"- **{count}x** {issue}")
    else:
        lines.append("- No automated judge findings.")
    lines.extend(
        [
            "",
            "## Failed scenario samples",
            "",
        ]
    )
    failures = [result for result in results if not result.passed]
    if not failures:
        lines.append("No strict failures.")
    else:
        for result in failures[:25]:
            lines.extend(
                [
                    f"### {result.name}",
                    "",
                    f"- User: `{result.user_text}`",
                    f"- Injected model fault: `{result.injected_model_fault or 'none'}`",
                    f"- Issues: {'; '.join(result.issues)}",
                    f"- Last response: `{result.messages[-1] if result.messages else '(none)'}`",
                    "",
                ]
            )
    lines.extend(
        [
            "## Scope and limitations",
            "",
            "- Covered: authenticated Telegram text webhook ingestion, private/group messages, "
            "multiple organization members, durable source events and conversations, exact and "
            "fuzzy catalog reads, guarded add/deduct tools, new single/bulk catalog workflows, "
            "duplicate and already-used SKU recovery, transaction retrieval by rough time, "
            "product, and relative recency, typed and button confirmation/cancellation, outbox "
            "rendering/delivery, duplicate updates, ledger application, and full reversals.",
            "- Transaction retrieval by creator/confirmer identity is exercised and reported as "
            "unsupported because the current authoritative transaction-read contract does not "
            "return either identity.",
            "- Model outputs are randomized schema-compatible simulations. This tests application "
            "containment and orchestration, not real-model language accuracy.",
            "- Telegram delivery is recorded in-process; Telegram's public API and client "
            "rendering are not contacted.",
            "- Invoice storage/extraction remains covered by its local-Supabase component test, "
            "not this randomized text stress run.",
            "- Speech transcription is not implemented in the current product and therefore cannot "
            "be stress-tested end to end yet.",
            "",
            "## Reproduce",
            "",
            "Stop the live worker first so it cannot claim stress events with the real model, "
            "then:",
            "",
            "```bash",
            (
                "uv run python -m inventory_agent.evaluation.pipeline_stress "
                f"--scenarios {report.requested_scenarios} "
                f"--users {report.simulated_users} --seed {report.seed}"
            ),
            "```",
            "",
            "The command requires local Supabase and intentionally retains a new stress-test "
            "organization for dashboard inspection.",
        ]
    )
    return "\n".join(lines) + "\n"


async def run_stress(
    *,
    scenarios: int,
    users: int,
    seed: int,
    report_path: Path,
) -> StressRunReport:
    runner = PipelineStressRunner(
        settings=Settings(),
        seed=seed,
        scenario_count=scenarios,
        user_count=users,
    )
    try:
        report = await runner.run()
    finally:
        await runner.close()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(report), encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run more than 100 no-credit scenarios through the complete local pipeline"
    )
    parser.add_argument("--scenarios", type=int, default=150)
    parser.add_argument("--users", type=int, default=24)
    parser.add_argument("--seed", type=int, default=20260726)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/testing/PIPELINE_STRESS_REPORT.md"),
    )
    args = parser.parse_args(argv)
    if args.scenarios < 101:
        parser.error("--scenarios must be at least 101")
    if args.users < 3:
        parser.error("--users must be at least 3")
    report = asyncio.run(
        run_stress(
            scenarios=args.scenarios,
            users=args.users,
            seed=args.seed,
            report_path=args.report,
        )
    )
    strict_passes = sum(result.passed for result in report.results)
    print(
        f"stress_complete scenarios={len(report.results)} strict_passes={strict_passes} "
        f"organization_id={report.organization_id} report={args.report}"
    )


def _required_plan(plan: ScenarioPlan | None) -> ScenarioPlan:
    if plan is None:
        raise RuntimeError("scenario model was called before prepare")
    return plan


def _tool_turn(plan: ScenarioPlan, round_number: int, call: FunctionCall) -> ModelTurn:
    return ModelTurn(
        response_id=f"stress-{plan.number}-{round_number}",
        model="stress-agent-simulator",
        output_items=[
            {
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, separators=(",", ":")),
            }
        ],
        output_text="",
        function_calls=[call],
        input_tokens=50,
        output_tokens=20,
        total_tokens=70,
    )


def _text_turn(plan: ScenarioPlan, round_number: int, text: str) -> ModelTurn:
    return ModelTurn(
        response_id=f"stress-{plan.number}-{round_number}",
        model="stress-agent-simulator",
        output_items=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        output_text=text,
        function_calls=[],
        input_tokens=60,
        output_tokens=30,
        total_tokens=90,
    )


def _proposal_note(plan: ScenarioPlan) -> str:
    if plan.assistant_style is AssistantStyle.PREMATURE_SUCCESS:
        return "Done! Inventory has been updated successfully."
    if plan.assistant_style is AssistantStyle.BROKEN_MARKDOWN:
        return "**Proposal prepared but this emphasis is malformed."
    if plan.assistant_style is AssistantStyle.VERBOSE:
        return (
            "I have carefully considered the inventory request and prepared a reviewable "
            "proposal. The inventory ledger is an important source of truth, so please spend "
            "some time checking every displayed product, quantity, unit, and identifier before "
            "deciding whether the proposed operation reflects the physical goods. You may then "
            "use the available confirmation or cancellation control."
        )
    operation = "deduction" if plan.kind is ScenarioKind.DEDUCT else "addition"
    return f"I prepared the stock {operation}. Please review and confirm or cancel."


def _latest_tool_output(input_items: list[dict[str, object]]) -> dict[str, object]:
    for item in reversed(input_items):
        if item.get("type") != "function_call_output":
            continue
        raw = item.get("output")
        if isinstance(raw, str):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    raise RuntimeError("simulated model did not receive a tool output")


def _first_on_hand(output: dict[str, object]) -> str:
    items = output.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return "0"
    return str(items[0].get("on_hand", "0"))


def _first_transaction_ref(output: dict[str, object]) -> str:
    transactions = output.get("transactions")
    if (
        not isinstance(transactions, list)
        or not transactions
        or not isinstance(transactions[0], dict)
    ):
        raise RuntimeError("transaction lookup returned no reference")
    value = transactions[0].get("transaction_ref")
    if not isinstance(value, str):
        raise RuntimeError("transaction lookup omitted transaction_ref")
    return value


def _variant_name(item_name: str, attributes: dict[str, str]) -> str | None:
    colour = attributes.get("colour")
    size = attributes.get("size")
    if colour and size:
        return f"{item_name} - {colour.title()} / {size}"
    return None


def _new_items_for(
    *,
    kind: ScenarioKind,
    number: int,
    quantity: Decimal,
) -> list[dict[str, object]]:
    if kind not in {
        ScenarioKind.NEW_SINGLE,
        ScenarioKind.NEW_BATCH,
        ScenarioKind.NEW_BATCH_DUPLICATE_SKU,
        ScenarioKind.NEW_BATCH_EXISTING_SKU,
    }:
        return []
    count = 1 if kind is ScenarioKind.NEW_SINGLE else 2
    return [
        {
            "name": f"Stress Sensor {number}-{index}",
            "sku": f"STRESS-SENSOR-{number:03d}-{index}",
            "model_sku": (
                None if (number + index) % 3 == 0 else f"STRESS-SENSOR-{number:03d}-{index}"
            ),
            "quantity": quantity + index - 1,
            "attributes": [
                {"key": "channel", "value": str(index)},
                {"key": "stress_seed_line", "value": f"{number}-{index}"},
            ],
        }
        for index in range(1, count + 1)
    ]


def _random_user_text(
    *,
    rng: random.Random,
    kind: ScenarioKind,
    target: TestVariant,
    quantity: Decimal,
    group_chat: bool,
) -> str:
    filler = rng.choice(("", "hey, ", "ok hmm ", "pls ", "warehouse update: "))
    typo_sku = target.sku
    if rng.random() < 0.18 and len(typo_sku) > 4:
        position = rng.randrange(1, len(typo_sku) - 1)
        typo_sku = typo_sku[:position] + typo_sku[position + 1 :]
    if kind is ScenarioKind.READ:
        body = rng.choice(
            (
                f"how many {target.item_name} do we have?",
                f"check stock for {target.sku}",
                f"tell me qty of {target.item_name} incl sku",
            )
        )
    elif kind is ScenarioKind.DEDUCT:
        body = rng.choice(
            (
                f"sold {quantity} {typo_sku}",
                f"take {quantity} of {target.item_name} out of stock",
                f"issue {quantity} units, code {target.sku}",
            )
        )
    elif kind is ScenarioKind.CANCEL:
        body = f"received {quantity} of {target.sku}, prepare it but i may cancel"
    elif kind is ScenarioKind.REVERSAL:
        body = "one of my latest receipts is wrong, help me reverse it"
    elif kind is ScenarioKind.NEW_SINGLE:
        body = f"received {quantity} totally new stress sensor"
    elif kind in {
        ScenarioKind.NEW_BATCH,
        ScenarioKind.NEW_BATCH_DUPLICATE_SKU,
        ScenarioKind.NEW_BATCH_EXISTING_SKU,
    }:
        body = f"new delivery: {quantity} sensor A and {quantity + 1} sensor B"
    elif kind in {
        ScenarioKind.TRANSACTION_BY_TIME,
        ScenarioKind.TRANSACTION_BY_PRODUCT,
        ScenarioKind.TRANSACTION_BY_ACTOR,
        ScenarioKind.TRANSACTION_N_AGO,
    }:
        body = "find one of my earlier inventory transactions"
    elif kind in {
        ScenarioKind.UNSAFE_UNGROUNDED,
        ScenarioKind.UNSAFE_NEGATIVE,
        ScenarioKind.WRONG_OPERATION,
    }:
        body = f"received {quantity} {target.sku}"
    else:
        body = rng.choice(
            (
                f"received {quantity} {typo_sku}",
                f"delivery here, got {quantity} of {target.item_name}",
                f"add {quantity} units part no {target.sku}",
            )
        )
    text = f"{filler}{body}"
    return f"@stressbot {text}" if group_chat else text


def _reversal_text(rng: random.Random, transaction_id: UUID) -> str:
    return rng.choice(
        (
            f"reverse transaction {transaction_id}, stress correction",
            f"undo {transaction_id} because that test receipt was wrong",
            f"pls reverse exact transaction id {transaction_id}",
        )
    )


def _callback_for(message: SentMessage, action: CallbackAction) -> str:
    for row in message.keyboard or []:
        for button in row:
            callback_data = button.get("callback_data")
            if not isinstance(callback_data, str):
                continue
            try:
                command = decode_callback(callback_data)
            except ValueError:
                continue
            if command.action is action:
                return callback_data
    labels = [button.get("text") for row in message.keyboard or [] for button in row]
    raise RuntimeError(f"message has no {action.name} callback; available={labels}")


def _has_action(message: SentMessage, action: CallbackAction) -> bool:
    try:
        _callback_for(message, action)
    except RuntimeError:
        return False
    return True


def _transaction_id_from_message(text: str) -> UUID | None:
    match = re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        text,
        flags=re.IGNORECASE,
    )
    return UUID(match.group(0)) if match else None


def _judge_ux(plan: ScenarioPlan, messages: list[str]) -> list[str]:
    issues: list[str] = []
    if not messages:
        return ["No Telegram response was produced"]
    for message in messages:
        if message.count("**") % 2:
            issues.append("Telegram response contains unbalanced bold Markdown")
            break
    if plan.assistant_style is AssistantStyle.PREMATURE_SUCCESS and any(
        "inventory has been updated" in message.casefold()
        and ("pending" in message.casefold() or "review" in message.casefold())
        for message in messages
    ):
        issues.append("Pending review includes a contradictory premature-success agent note")
    if plan.assistant_style is AssistantStyle.VERBOSE and any(
        len(message) > 1_200 for message in messages
    ):
        issues.append("Telegram review is excessively long")
    if plan.kind in {
        ScenarioKind.ADD,
        ScenarioKind.DEDUCT,
        ScenarioKind.CANCEL,
        ScenarioKind.WRONG_OPERATION,
    } and not any(("Confirm" in message and "Cancel" in message) for message in messages):
        issues.append("Stock proposal never displayed clear Confirm and Cancel guidance")
    if plan.kind in {
        ScenarioKind.NEW_BATCH,
        ScenarioKind.NEW_BATCH_DUPLICATE_SKU,
        ScenarioKind.NEW_BATCH_EXISTING_SKU,
    } and not any("SKU needed" in message or "CREATE + ADD" in message for message in messages):
        issues.append("Bulk new-item workflow did not visibly request details or review creation")
    return sorted(set(issues))


def _injected_fault(plan: ScenarioPlan) -> str | None:
    if plan.kind is ScenarioKind.UNSAFE_UNGROUNDED:
        return "ungrounded variant UUID"
    if plan.kind is ScenarioKind.UNSAFE_NEGATIVE:
        return "negative quantity"
    if plan.kind is ScenarioKind.WRONG_OPERATION:
        return "semantic intent inversion"
    if plan.assistant_style is AssistantStyle.PREMATURE_SUCCESS:
        return "premature success prose"
    if plan.assistant_style is AssistantStyle.BROKEN_MARKDOWN:
        return "unbalanced Telegram Markdown"
    if plan.assistant_style is AssistantStyle.VERBOSE:
        return "verbose proposal prose"
    return None


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000


def _percent(value: int, total: int) -> str:
    return f"{(value / total * 100) if total else 0:.1f}%"


def _finding_label(issue: str) -> str:
    if issue.startswith("Expected balance "):
        return "Applied balance differed from the user-requested operation"
    return issue


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


if __name__ == "__main__":
    main()
