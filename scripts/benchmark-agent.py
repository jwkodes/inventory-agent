"""Run a small, billable agent benchmark without sending Telegram messages."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from time import perf_counter
from uuid import UUID, uuid4

import httpx
from openai import AsyncOpenAI

from inventory_agent.agent.context import (
    AgentContextManager,
    ContextRetentionPolicy,
    ContextRetentionSettings,
    ModelConversationSummarizer,
)
from inventory_agent.agent.production_tools import (
    GroundedAgentCatalogReader,
)
from inventory_agent.agent.repository import SupabaseAgentRepository
from inventory_agent.agent.runtime import OpenAIResponsesAgentModel
from inventory_agent.catalog.interpreter import OpenAICatalogDetailsInterpreter
from inventory_agent.catalog.repository import SupabaseCatalogItemCreationRepository
from inventory_agent.config import Settings
from inventory_agent.matching.repository import SupabaseInventoryCandidateRepository
from inventory_agent.matching.semantic import (
    OpenAIEmbeddingProvider,
    SupabaseSemanticCandidateRepository,
)
from inventory_agent.matching.service import MatchingStrategy
from inventory_agent.processing.agent_text_events import TelegramAgentTextEventProcessor
from inventory_agent.processing.repository import (
    SupabaseProcessingOutboxRepository,
    SupabaseSourceEventWorkRepository,
)
from inventory_agent.proposals.actions import SupabaseProposalActionRepository
from inventory_agent.proposals.repository import SupabaseProposalRepository
from inventory_agent.reversals.repository import SupabaseReversalRepository

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")


def _secret(value: object, name: str) -> str:
    getter = getattr(value, "get_secret_value", None)
    secret = getter() if callable(getter) else None
    if not isinstance(secret, str) or not secret:
        raise RuntimeError(f"{name} is required")
    return secret


async def main() -> None:
    settings = Settings()
    if settings.supabase_url not in {"http://127.0.0.1:54321", "http://localhost:54321"}:
        raise RuntimeError("The benchmark is restricted to local Supabase")
    secret_key = _secret(settings.supabase_secret_key, "SUPABASE_SECRET_KEY")
    openai_key = _secret(settings.openai_api_key, "OPENAI_API_KEY")
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    rest_url = f"{settings.supabase_url.rstrip('/')}/rest/v1"
    chat_id = -(7_000_000_000 + uuid4().int % 1_000_000_000)
    event_ids: list[UUID] = []
    cleanup_event_ids: list[UUID] = []

    async with AsyncExitStack() as stack:
        database = await stack.enter_async_context(
            httpx.AsyncClient(base_url=rest_url, headers=headers, timeout=30)
        )
        openai = await stack.enter_async_context(AsyncOpenAI(api_key=openai_key))
        member_response = await database.get(
            "/organization_users",
            params={
                "select": "telegram_user_id",
                "organization_id": f"eq.{ORGANIZATION_ID}",
                "id": f"eq.{ACTOR_ID}",
                "active": "eq.true",
                "limit": "1",
            },
        )
        member_response.raise_for_status()
        members = member_response.json()
        if not members:
            raise RuntimeError("Benchmark actor is unavailable")
        telegram_user_id = int(members[0]["telegram_user_id"])

        latest_response = await database.get(
            "/inventory_transactions",
            params={
                "select": "id",
                "organization_id": f"eq.{ORGANIZATION_ID}",
                "status": "eq.applied",
                "order": "applied_at.desc",
                "limit": "1",
            },
        )
        latest_response.raise_for_status()
        latest_rows = latest_response.json()

        agent_repository = SupabaseAgentRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        model = OpenAIResponsesAgentModel(
            client=openai,
            model=settings.inventory_agent_model,
            reasoning_effort=settings.inventory_agent_reasoning_effort,
        )
        candidates = SupabaseInventoryCandidateRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        semantic = SupabaseSemanticCandidateRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
            embeddings=OpenAIEmbeddingProvider(
                client=openai,
                model=settings.openai_embedding_model,
                dimensions=settings.openai_embedding_dimensions,
            ),
            embedding_model=settings.openai_embedding_model,
            embedding_dimensions=settings.openai_embedding_dimensions,
        )
        proposal_actions = SupabaseProposalActionRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        reversals = SupabaseReversalRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        processor = TelegramAgentTextEventProcessor(
            events=SupabaseSourceEventWorkRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            ),
            model=model,
            conversations=agent_repository,
            catalog_reader=GroundedAgentCatalogReader(
                candidates=candidates,
                semantic=semantic,
                reads=agent_repository,
                strategy=MatchingStrategy(settings.inventory_matching_strategy),
            ),
            reads=agent_repository,
            proposals=SupabaseProposalRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            ),
            proposal_actions=proposal_actions,
            outbox=SupabaseProcessingOutboxRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            ),
            reversals=reversals,
            catalog=SupabaseCatalogItemCreationRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            ),
            catalog_interpreter=OpenAICatalogDetailsInterpreter(
                client=openai,
                model=settings.openai_model,
                reasoning_effort=settings.openai_reasoning_effort,
            ),
            context_manager=AgentContextManager(
                conversations=agent_repository,
                defaults=ContextRetentionSettings(
                    policy=ContextRetentionPolicy(settings.inventory_agent_context_policy),
                    retention_days=settings.inventory_agent_context_retention_days,
                    max_tokens=settings.inventory_agent_context_max_tokens,
                    max_items=settings.inventory_agent_context_max_items,
                ),
                settings_provider=agent_repository,
                summarizer=ModelConversationSummarizer(model=model),
            ),
            bot_username=settings.telegram_bot_username,
        )

        scenarios = [
            (
                "semantic_inventory",
                "How many Classic T-Shirts do we have? List each variant with its SKU.",
            ),
            (
                "recent_transactions",
                "Show the last five inventory transactions with their IDs, stored types, "
                "statuses, timestamps, and summaries.",
            ),
        ]
        if latest_rows:
            scenarios.append(
                (
                    "exact_transaction_uuid",
                    f"Show me the stored details for transaction {latest_rows[0]['id']}.",
                )
            )

        message_number = 0

        async def run_message(name: str, message: str, *, durable: bool = False) -> object:
            nonlocal message_number
            message_number += 1
            event_id = uuid4()
            event_ids.append(event_id)
            if not durable:
                cleanup_event_ids.append(event_id)
            create = await database.post(
                "/source_events",
                headers={"Prefer": "return=minimal"},
                json={
                    "id": str(event_id),
                    "organization_id": str(ORGANIZATION_ID),
                    "provider": "telegram",
                    "external_event_id": f"benchmark-{event_id}",
                    "event_type": "message",
                    "payload": {
                        "update_id": 9_000_000 + message_number,
                        "message": {
                            "message_id": 9_000_000 + message_number,
                            "from": {"id": telegram_user_id},
                            "chat": {"id": chat_id},
                            "text": message,
                        },
                    },
                },
            )
            create.raise_for_status()
            started = perf_counter()
            result = await processor.process(event_id)
            duration_ms = (perf_counter() - started) * 1000
            conversation = await agent_repository.load(
                organization_id=ORGANIZATION_ID,
                organization_user_id=ACTOR_ID,
                chat_id=chat_id,
            )
            print(
                f"benchmark_result scenario={name} duration_ms={duration_ms:.2f} "
                f"status={result.status} model={conversation.model_name}"
            )
            print(f"benchmark_reply scenario={name} text={conversation.last_reply_text!r}")
            return result

        try:
            for name, message in scenarios:
                await run_message(name, message)

            variant_response = await database.get(
                "/item_variants",
                params={
                    "select": "sku",
                    "organization_id": f"eq.{ORGANIZATION_ID}",
                    "active": "eq.true",
                    "sku": "not.is.null",
                    "order": "created_at.asc",
                    "limit": "1",
                },
            )
            variant_response.raise_for_status()
            variants = variant_response.json()
            if not variants:
                raise RuntimeError("No benchmarkable SKU is available")
            sku = str(variants[0]["sku"])

            receipt = await run_message(
                "stock_receipt_proposal",
                f"Received 1 of SKU {sku} for an end-to-end latency benchmark.",
                durable=True,
            )
            proposal_id = getattr(receipt, "proposal_id", None)
            if not isinstance(proposal_id, UUID):
                raise RuntimeError("The live model did not create the benchmark receipt")
            confirm_started = perf_counter()
            transaction_id = await proposal_actions.confirm(
                proposal_id=proposal_id,
                actor_id=ACTOR_ID,
            )
            print(
                "benchmark_result scenario=stock_receipt_confirm "
                f"duration_ms={(perf_counter() - confirm_started) * 1000:.2f} "
                f"transaction_id={transaction_id}"
            )

            reversal = await run_message(
                "exact_uuid_reversal_proposal",
                f"Reverse transaction {transaction_id}. Reason: undo the latency benchmark.",
                durable=True,
            )
            request_id = getattr(reversal, "reversal_request_id", None)
            if not isinstance(request_id, UUID):
                raise RuntimeError("The live model did not create the benchmark reversal")
            reversal_started = perf_counter()
            reversal_transaction_id = await reversals.confirm(
                request_id=request_id,
                actor_id=ACTOR_ID,
            )
            print(
                "benchmark_result scenario=reversal_confirm "
                f"duration_ms={(perf_counter() - reversal_started) * 1000:.2f} "
                f"transaction_id={reversal_transaction_id} net_stock_change=0"
            )
        finally:
            await processor.wait_for_background_compactions()
            if event_ids:
                await database.delete(
                    "/processing_outbox",
                    params={
                        "source_event_id": f"in.({','.join(str(value) for value in event_ids)})"
                    },
                )
            await database.delete(
                "/inventory_agent_conversations",
                params={"chat_id": f"eq.{chat_id}"},
            )
            if cleanup_event_ids:
                await database.delete(
                    "/source_events",
                    params={"id": f"in.({','.join(str(value) for value in cleanup_event_ids)})"},
                )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
