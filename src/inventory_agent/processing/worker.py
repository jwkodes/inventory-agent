"""Command-line worker for Telegram callbacks, text processing, and delivery."""

import argparse
import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

from openai import AsyncOpenAI
from pydantic import SecretStr

from inventory_agent.config import Settings
from inventory_agent.extraction.interpreter import OpenAITextCommandInterpreter
from inventory_agent.matching.repository import SupabaseInventoryCandidateRepository
from inventory_agent.matching.service import InventoryItemMatcher
from inventory_agent.processing.callback_events import (
    CallbackEventProcessingError,
    CallbackEventProcessingResult,
    TelegramCallbackEventProcessor,
)
from inventory_agent.processing.delivery import TelegramOutboxDeliveryWorker
from inventory_agent.processing.models import (
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

logger = logging.getLogger(__name__)


class NextTextEventProcessor(Protocol):
    async def process_next(self) -> TextEventProcessingResult | None:
        """Process at most one eligible text event."""


class NextCallbackEventProcessor(Protocol):
    async def process_next(self) -> CallbackEventProcessingResult | None:
        """Process at most one eligible callback event."""


class NextOutboxDeliveryWorker(Protocol):
    async def deliver_one(self) -> OutboxDeliveryResult:
        """Deliver at most one due outbound outcome."""


async def run_loop(
    *,
    callback_processor: NextCallbackEventProcessor,
    text_processor: NextTextEventProcessor,
    delivery_worker: NextOutboxDeliveryWorker,
    watch: bool,
    poll_seconds: float,
) -> None:
    """Prioritize button actions, then process text and outbound delivery."""

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

        text_result: TextEventProcessingResult | None = None
        try:
            text_result = await text_processor.process_next()
        except TextEventProcessingError:
            logger.error("text_event_processing status=failed")
        if text_result is not None:
            logger.info(
                "text_event_processing status=%s event_id=%s proposal_id=%s",
                text_result.status,
                text_result.event_id,
                text_result.proposal_id,
            )

        delivery_result = await delivery_worker.deliver_one()
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
            and text_result is None
            and delivery_result.status is OutboxDeliveryStatus.IDLE
        ):
            await asyncio.sleep(poll_seconds)


async def run_worker(*, watch: bool, poll_seconds: float) -> None:
    settings = Settings()
    secret_key = _required_secret(settings.supabase_secret_key, "SUPABASE_SECRET_KEY")
    bot_token = _required_secret(settings.telegram_bot_token, "TELEGRAM_BOT_TOKEN")
    openai_api_key = _required_secret(settings.openai_api_key, "OPENAI_API_KEY")
    openai_client = AsyncOpenAI(api_key=openai_api_key)
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
        callback_processor = TelegramCallbackEventProcessor(
            events=event_repository,
            dispatcher=TelegramCallbackDispatcher(
                answerer=telegram_client,
                repository=SupabaseProposalActionRepository(
                    supabase_url=settings.supabase_url,
                    secret_key=secret_key,
                ),
                reversals=reversal_repository,
            ),
            proposal_views=proposal_view_repository,
            message_editor=telegram_client,
        )
        text_processor = TelegramTextEventProcessor(
            events=event_repository,
            interpreter=OpenAITextCommandInterpreter(
                client=openai_client,
                model=settings.openai_model,
                reasoning_effort=settings.openai_reasoning_effort,
            ),
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
            reversals=reversal_repository,
        )
        delivery_worker = TelegramOutboxDeliveryWorker(
            repository=proposal_view_repository,
            sender=telegram_client,
        )
        await run_loop(
            callback_processor=callback_processor,
            text_processor=text_processor,
            delivery_worker=delivery_worker,
            watch=watch,
            poll_seconds=poll_seconds,
        )
    finally:
        await openai_client.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Process Telegram callbacks and inventory text events, then deliver outcomes"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep polling instead of running one callback, text, and delivery cycle",
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
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(watch=args.watch, poll_seconds=args.poll_seconds))


def _required_secret(secret: SecretStr | None, variable_name: str) -> str:
    value = secret.get_secret_value() if secret is not None else ""
    if not value:
        raise RuntimeError(f"{variable_name} is required by the worker")
    return value


if __name__ == "__main__":
    main()
