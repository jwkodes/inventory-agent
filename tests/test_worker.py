"""Tests for the unified text-processing and delivery worker cycle."""

from uuid import UUID

from inventory_agent.processing.callback_events import CallbackEventProcessingResult
from inventory_agent.processing.models import (
    ImageEventProcessingResult,
    OutboxDeliveryResult,
    OutboxDeliveryStatus,
    TextEventProcessingResult,
    TextEventProcessingStatus,
)
from inventory_agent.processing.text_events import TextEventProcessingError
from inventory_agent.processing.worker import run_loop
from inventory_agent.telegram.callback_dispatcher import (
    CallbackOutcome,
    CallbackOutcomeStatus,
)

EVENT_ID = UUID("50000000-0000-0000-0000-000000000009")
OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000009")


class FakeTextProcessor:
    def __init__(
        self,
        events: list[str],
        result: TextEventProcessingResult | Exception | None,
    ) -> None:
        self.events = events
        self.result = result

    async def process_next(self) -> TextEventProcessingResult | None:
        self.events.append("text")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeImageProcessor:
    def __init__(
        self,
        events: list[str],
        result: ImageEventProcessingResult | Exception | None = None,
    ) -> None:
        self.events = events
        self.result = result

    async def process_next(self) -> ImageEventProcessingResult | None:
        self.events.append("image")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeCallbackProcessor:
    def __init__(
        self,
        events: list[str],
        result: CallbackEventProcessingResult | Exception | None = None,
    ) -> None:
        self.events = events
        self.result = result

    async def process_next(self) -> CallbackEventProcessingResult | None:
        self.events.append("callback")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeDeliveryWorker:
    def __init__(self, events: list[str], result: OutboxDeliveryResult) -> None:
        self.events = events
        self.result = result

    async def deliver_one(self) -> OutboxDeliveryResult:
        self.events.append("delivery")
        return self.result


async def test_one_cycle_processes_text_before_delivering_its_outcome() -> None:
    events: list[str] = []
    await run_loop(
        callback_processor=FakeCallbackProcessor(events),
        image_processor=FakeImageProcessor(events),
        text_processor=FakeTextProcessor(
            events,
            TextEventProcessingResult(
                event_id=EVENT_ID,
                status=TextEventProcessingStatus.PROPOSAL_READY,
                outbox_id=OUTBOX_ID,
            ),
        ),
        delivery_worker=FakeDeliveryWorker(
            events,
            OutboxDeliveryResult(
                status=OutboxDeliveryStatus.SENT,
                outbox_id=OUTBOX_ID,
                telegram_message_id=99,
            ),
        ),
        watch=False,
        poll_seconds=2,
    )

    assert events == ["callback", "image", "text", "delivery"]


async def test_text_failure_does_not_block_existing_outbox_delivery() -> None:
    events: list[str] = []
    await run_loop(
        callback_processor=FakeCallbackProcessor(events),
        image_processor=FakeImageProcessor(events),
        text_processor=FakeTextProcessor(
            events,
            TextEventProcessingError("recorded failure"),
        ),
        delivery_worker=FakeDeliveryWorker(
            events,
            OutboxDeliveryResult(status=OutboxDeliveryStatus.SENT, outbox_id=OUTBOX_ID),
        ),
        watch=False,
        poll_seconds=2,
    )

    assert events == ["callback", "image", "text", "delivery"]


async def test_idle_one_shot_cycle_returns_after_both_queues_are_empty() -> None:
    events: list[str] = []
    await run_loop(
        callback_processor=FakeCallbackProcessor(events),
        image_processor=FakeImageProcessor(events),
        text_processor=FakeTextProcessor(events, None),
        delivery_worker=FakeDeliveryWorker(
            events,
            OutboxDeliveryResult(status=OutboxDeliveryStatus.IDLE),
        ),
        watch=False,
        poll_seconds=2,
    )

    assert events == ["callback", "image", "text", "delivery"]


async def test_callback_is_processed_before_text_and_delivery() -> None:
    events: list[str] = []
    callback_result = CallbackEventProcessingResult(
        event_id=EVENT_ID,
        outcome=CallbackOutcome(
            CallbackOutcomeStatus.INVALID,
            None,
            None,
            "invalid",
        ),
    )

    await run_loop(
        callback_processor=FakeCallbackProcessor(events, callback_result),
        image_processor=FakeImageProcessor(events),
        text_processor=FakeTextProcessor(events, None),
        delivery_worker=FakeDeliveryWorker(
            events,
            OutboxDeliveryResult(status=OutboxDeliveryStatus.IDLE),
        ),
        watch=False,
        poll_seconds=2,
    )

    assert events == ["callback", "image", "text", "delivery"]
