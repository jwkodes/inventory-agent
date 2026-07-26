"""Command-line worker for Telegram callbacks, inputs, and delivery."""

import argparse
import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

from openai import AsyncOpenAI
from pydantic import SecretStr

from inventory_agent.agent.context import (
    AgentContextManager,
    ContextRetentionPolicy,
    ContextRetentionSettings,
    ModelConversationSummarizer,
)
from inventory_agent.agent.production_tools import GroundedAgentCatalogReader
from inventory_agent.agent.repository import SupabaseAgentRepository
from inventory_agent.agent.runtime import OpenAIResponsesAgentModel
from inventory_agent.artifacts.repository import SupabaseSourceArtifactRepository
from inventory_agent.catalog.interpreter import OpenAICatalogDetailsInterpreter
from inventory_agent.catalog.repository import SupabaseCatalogItemCreationRepository
from inventory_agent.config import Settings
from inventory_agent.extraction.image_interpreter import OpenAIImageCommandInterpreter
from inventory_agent.extraction.interpreter import OpenAITextCommandInterpreter
from inventory_agent.matching.clarification import (
    SupabaseMatchClarificationRepository,
)
from inventory_agent.matching.judge import OpenAICandidateJudge
from inventory_agent.matching.repository import SupabaseInventoryCandidateRepository
from inventory_agent.matching.semantic import (
    OpenAIEmbeddingProvider,
    SupabaseSemanticCandidateRepository,
)
from inventory_agent.matching.service import InventoryItemMatcher, MatchingStrategy
from inventory_agent.processing.agent_text_events import (
    AgentTextEventProcessingError,
    TelegramAgentTextEventProcessor,
)
from inventory_agent.processing.callback_events import (
    CallbackEventProcessingError,
    CallbackEventProcessingResult,
    TelegramCallbackEventProcessor,
)
from inventory_agent.processing.commands import InventoryCommandHandler
from inventory_agent.processing.delivery import TelegramOutboxDeliveryWorker
from inventory_agent.processing.image_events import (
    ImageEventProcessingError,
    TelegramImageEventProcessor,
)
from inventory_agent.processing.models import (
    ImageEventProcessingResult,
    OutboxDeliveryResult,
    OutboxDeliveryStatus,
    TextEventProcessingResult,
)
from inventory_agent.processing.repository import (
    SupabaseProcessingOutboxDeliveryRepository,
    SupabaseProcessingOutboxRepository,
    SupabaseSourceEventWorkRepository,
)
from inventory_agent.processing.text_events import (
    TelegramTextEventProcessor,
    TextEventProcessingError,
)
from inventory_agent.proposals.actions import SupabaseProposalActionRepository
from inventory_agent.proposals.repository import SupabaseProposalRepository
from inventory_agent.reversals.repository import SupabaseReversalRepository
from inventory_agent.telegram.callback_dispatcher import TelegramCallbackDispatcher
from inventory_agent.telegram.client import TelegramBotClient
from inventory_agent.telegram.registration import (
    SupabaseRegistrationRepository,
    TelegramRegistrationNotificationWorker,
)

logger = logging.getLogger(__name__)


class NextTextEventProcessor(Protocol):
    async def process_next(self) -> TextEventProcessingResult | None:
        """Process at most one eligible text event."""


class NextImageEventProcessor(Protocol):
    async def process_next(self) -> ImageEventProcessingResult | None:
        """Process at most one eligible invoice image event."""


class NextCallbackEventProcessor(Protocol):
    async def process_next(self) -> CallbackEventProcessingResult | None:
        """Process at most one eligible callback event."""


class NextOutboxDeliveryWorker(Protocol):
    async def deliver_one(self) -> OutboxDeliveryResult:
        """Deliver at most one due outbound outcome."""


async def run_loop(
    *,
    callback_processor: NextCallbackEventProcessor,
    image_processor: NextImageEventProcessor,
    text_processor: NextTextEventProcessor,
    registration_delivery_worker: NextOutboxDeliveryWorker,
    delivery_worker: NextOutboxDeliveryWorker,
    watch: bool,
    poll_seconds: float,
) -> None:
    """Process inputs, registration notices, and ordinary outbound delivery."""

    while True:
        callback_result: CallbackEventProcessingResult | None = None
        try:
            callback_result = await callback_processor.process_next()
        except CallbackEventProcessingError:
            logger.error("callback_event_processing status=failed")
        if callback_result is not None:
            logger.info(
                "callback_event_processing status=%s event_id=%s action=%s",
                callback_result.outcome.status,
                callback_result.event_id,
                callback_result.outcome.action,
            )

        image_result: ImageEventProcessingResult | None = None
        try:
            image_result = await image_processor.process_next()
        except ImageEventProcessingError:
            logger.error("image_event_processing status=failed")
        if image_result is not None:
            logger.info(
                "image_event_processing status=%s event_id=%s proposal_id=%s",
                image_result.status,
                image_result.event_id,
                image_result.proposal_id,
            )

        text_result: TextEventProcessingResult | None = None
        try:
            text_result = await text_processor.process_next()
        except (TextEventProcessingError, AgentTextEventProcessingError):
            logger.exception("text_event_processing status=failed")
        if text_result is not None:
            logger.info(
                "text_event_processing status=%s event_id=%s proposal_id=%s",
                text_result.status,
                text_result.event_id,
                text_result.proposal_id,
            )

        registration_delivery_result = await registration_delivery_worker.deliver_one()
        if registration_delivery_result.status is not OutboxDeliveryStatus.IDLE:
            logger.info(
                "registration_delivery status=%s notification_id=%s telegram_message_id=%s",
                registration_delivery_result.status,
                registration_delivery_result.outbox_id,
                registration_delivery_result.telegram_message_id,
            )

        delivery_result = await delivery_worker.deliver_one()
        if delivery_result.status is not OutboxDeliveryStatus.IDLE:
            logger.info(
                "outbox_delivery status=%s outbox_id=%s telegram_message_id=%s",
                delivery_result.status,
                delivery_result.outbox_id,
                delivery_result.telegram_message_id,
            )
        if not watch:
            return
        if (
            callback_result is None
            and image_result is None
            and text_result is None
            and registration_delivery_result.status is OutboxDeliveryStatus.IDLE
            and delivery_result.status is OutboxDeliveryStatus.IDLE
        ):
            await asyncio.sleep(poll_seconds)


async def run_worker(*, watch: bool, poll_seconds: float) -> None:
    settings = Settings()
    secret_key = _required_secret(settings.supabase_secret_key, "SUPABASE_SECRET_KEY")
    bot_token = _required_secret(settings.telegram_bot_token, "TELEGRAM_BOT_TOKEN")
    openai_api_key = _required_secret(settings.openai_api_key, "OPENAI_API_KEY")
    openai_client = AsyncOpenAI(api_key=openai_api_key)
    agent_text_processor: TelegramAgentTextEventProcessor | None = None
    try:
        event_repository = SupabaseSourceEventWorkRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        telegram_client = TelegramBotClient(bot_token=bot_token)
        proposal_view_repository = SupabaseProcessingOutboxDeliveryRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        reversal_repository = SupabaseReversalRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        catalog_repository = SupabaseCatalogItemCreationRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        outbox = SupabaseProcessingOutboxRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        agent_repository = (
            SupabaseAgentRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            )
            if settings.inventory_agent_enabled
            else None
        )
        proposal_actions = SupabaseProposalActionRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        callback_processor = TelegramCallbackEventProcessor(
            events=event_repository,
            dispatcher=TelegramCallbackDispatcher(
                answerer=telegram_client,
                repository=proposal_actions,
                reversals=reversal_repository,
                catalog=catalog_repository,
            ),
            message_editor=telegram_client,
            outbox=outbox,
            conversation_recorder=agent_repository,
        )
        candidate_judge = (
            OpenAICandidateJudge(
                client=openai_client,
                model=settings.openai_model,
                reasoning_effort=settings.openai_reasoning_effort,
            )
            if settings.inventory_candidate_judging_enabled
            else None
        )
        clarification_repository = SupabaseMatchClarificationRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        candidate_repository = SupabaseInventoryCandidateRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        semantic_repository = SupabaseSemanticCandidateRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
            embeddings=OpenAIEmbeddingProvider(
                client=openai_client,
                model=settings.openai_embedding_model,
                dimensions=settings.openai_embedding_dimensions,
            ),
            embedding_model=settings.openai_embedding_model,
            embedding_dimensions=settings.openai_embedding_dimensions,
        )
        matching_strategy = MatchingStrategy(settings.inventory_matching_strategy)
        matcher = InventoryItemMatcher(
            repository=candidate_repository,
            semantic_repository=semantic_repository,
            judge=candidate_judge,
            strategy=matching_strategy,
        )
        proposals = SupabaseProposalRepository(
            supabase_url=settings.supabase_url,
            secret_key=secret_key,
        )
        command_handler = InventoryCommandHandler(
            matcher=matcher,
            proposals=proposals,
            outbox=outbox,
            clarifications=clarification_repository,
        )
        catalog_interpreter = OpenAICatalogDetailsInterpreter(
            client=openai_client,
            model=settings.openai_model,
            reasoning_effort=settings.openai_reasoning_effort,
        )
        if settings.inventory_agent_enabled:
            if agent_repository is None:  # pragma: no cover - construction invariant
                raise RuntimeError("Inventory agent repository is unavailable")
            agent_model = OpenAIResponsesAgentModel(
                client=openai_client,
                model=settings.inventory_agent_model,
                reasoning_effort=settings.inventory_agent_reasoning_effort,
            )
            agent_text_processor = TelegramAgentTextEventProcessor(
                events=event_repository,
                model=agent_model,
                conversations=agent_repository,
                catalog_reader=GroundedAgentCatalogReader(
                    candidates=candidate_repository,
                    semantic=semantic_repository,
                    reads=agent_repository,
                    strategy=matching_strategy,
                ),
                reads=agent_repository,
                proposals=proposals,
                proposal_actions=proposal_actions,
                outbox=outbox,
                reversals=reversal_repository,
                catalog=catalog_repository,
                catalog_interpreter=catalog_interpreter,
                bot_username=settings.telegram_bot_username,
                context_manager=AgentContextManager(
                    conversations=agent_repository,
                    defaults=ContextRetentionSettings(
                        policy=ContextRetentionPolicy(settings.inventory_agent_context_policy),
                        retention_days=settings.inventory_agent_context_retention_days,
                        max_tokens=settings.inventory_agent_context_max_tokens,
                        max_items=settings.inventory_agent_context_max_items,
                    ),
                    settings_provider=agent_repository,
                    summarizer=ModelConversationSummarizer(model=agent_model),
                ),
            )
            text_processor: NextTextEventProcessor = agent_text_processor
        else:
            text_processor = TelegramTextEventProcessor(
                events=event_repository,
                interpreter=OpenAITextCommandInterpreter(
                    client=openai_client,
                    model=settings.openai_model,
                    reasoning_effort=settings.openai_reasoning_effort,
                ),
                catalog_interpreter=catalog_interpreter,
                matcher=matcher,
                proposals=proposals,
                outbox=outbox,
                reversals=reversal_repository,
                catalog=catalog_repository,
                clarifications=clarification_repository,
                candidate_judge=candidate_judge,
                bot_username=settings.telegram_bot_username,
            )
        image_processor = TelegramImageEventProcessor(
            events=event_repository,
            downloader=telegram_client,
            artifacts=SupabaseSourceArtifactRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
                bucket=settings.supabase_storage_bucket,
            ),
            interpreter=OpenAIImageCommandInterpreter(
                client=openai_client,
                model=settings.openai_model,
                reasoning_effort=settings.openai_reasoning_effort,
            ),
            commands=command_handler,
            bot_username=settings.telegram_bot_username,
        )
        delivery_worker = TelegramOutboxDeliveryWorker(
            repository=proposal_view_repository,
            sender=telegram_client,
            display_timezone=settings.inventory_display_timezone,
        )
        registration_delivery_worker = TelegramRegistrationNotificationWorker(
            repository=SupabaseRegistrationRepository(
                supabase_url=settings.supabase_url,
                secret_key=secret_key,
            ),
            sender=telegram_client,
        )
        await run_loop(
            callback_processor=callback_processor,
            image_processor=image_processor,
            text_processor=text_processor,
            registration_delivery_worker=registration_delivery_worker,
            delivery_worker=delivery_worker,
            watch=watch,
            poll_seconds=poll_seconds,
        )
    finally:
        if agent_text_processor is not None:
            await agent_text_processor.wait_for_background_compactions()
        await openai_client.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Process Telegram callbacks and inventory inputs, then deliver outcomes"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep polling instead of running one callback, image, text, and delivery cycle",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="Idle polling interval when --watch is enabled (default: 2)",
    )
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0 or args.poll_seconds > 60:
        parser.error("--poll-seconds must be greater than 0 and no more than 60")
    _configure_logging()
    asyncio.run(run_worker(watch=args.watch, poll_seconds=args.poll_seconds))


def _configure_logging() -> None:
    """Keep provider URLs containing credentials out of application logs."""

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _required_secret(secret: SecretStr | None, variable_name: str) -> str:
    value = secret.get_secret_value() if secret is not None else ""
    if not value:
        raise RuntimeError(f"{variable_name} is required by the worker")
    return value


if __name__ == "__main__":
    main()
