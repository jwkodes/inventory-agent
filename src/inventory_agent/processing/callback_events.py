"""Process durable Telegram callback events and retire their source controls."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from inventory_agent.processing.models import (
    ProcessingOutcomeDraft,
    ProcessingOutcomeType,
    TelegramCallbackEventContext,
)
from inventory_agent.telegram.callback_dispatcher import (
    CallbackOutcome,
    CallbackOutcomeStatus,
)
from inventory_agent.telegram.callbacks import CallbackAction


class CallbackDispatcher(Protocol):
    async def dispatch(
        self,
        *,
        callback_query_id: str,
        callback_data: str,
        actor_id: UUID,
        chat_id: int,
    ) -> CallbackOutcome:
        """Acknowledge and execute one callback action."""


class CallbackEventRepository(Protocol):
    async def claim_next_callback_event(self) -> TelegramCallbackEventContext | None:
        """Claim the oldest eligible callback."""

    async def finish_event(
        self,
        *,
        event_id: UUID,
        success: bool,
        error_message: str | None = None,
    ) -> bool:
        """Complete or retry one callback source event."""


class ProcessingOutbox(Protocol):
    async def enqueue(self, draft: ProcessingOutcomeDraft) -> UUID:
        """Persist one idempotent outbound Telegram message."""


class TelegramMessageEditor(Protocol):
    async def remove_inline_keyboard(self, *, chat_id: int, message_id: int) -> None:
        """Remove controls without replacing the message that originated a callback."""


@dataclass(frozen=True, slots=True)
class CallbackEventProcessingResult:
    event_id: UUID
    outcome: CallbackOutcome


class CallbackEventProcessingError(RuntimeError):
    """A callback attempt failed after its source event was claimed."""


class TelegramCallbackEventProcessor:
    def __init__(
        self,
        *,
        events: CallbackEventRepository,
        dispatcher: CallbackDispatcher,
        message_editor: TelegramMessageEditor,
        outbox: ProcessingOutbox,
    ) -> None:
        self._events = events
        self._dispatcher = dispatcher
        self._message_editor = message_editor
        self._outbox = outbox

    async def process_next(self) -> CallbackEventProcessingResult | None:
        context = await self._events.claim_next_callback_event()
        if context is None:
            return None

        try:
            outcome = await self._dispatcher.dispatch(
                callback_query_id=context.callback_query_id,
                callback_data=context.callback_data,
                actor_id=context.organization_user_id,
                chat_id=context.chat_id,
            )
            if outcome.status is CallbackOutcomeStatus.FAILED:
                raise RuntimeError("Callback database action failed")

            if outcome.status is CallbackOutcomeStatus.COMPLETED:
                await self._publish_completed_action(
                    action=outcome.action,
                    result_id=outcome.result_id,
                    event_id=context.event_id,
                    organization_id=context.organization_id,
                    chat_id=context.chat_id,
                    message_id=context.telegram_message_id,
                    catalog_status=outcome.catalog_status,
                )
            if not await self._events.finish_event(event_id=context.event_id, success=True):
                raise RuntimeError("Claimed callback event could not be completed")
            return CallbackEventProcessingResult(event_id=context.event_id, outcome=outcome)
        except Exception as error:
            failure = f"{type(error).__name__}: callback event processing failed"
            try:
                await self._events.finish_event(
                    event_id=context.event_id,
                    success=False,
                    error_message=failure,
                )
            except Exception as finish_error:
                raise CallbackEventProcessingError(
                    "Callback processing and failure recording both failed"
                ) from finish_error
            raise CallbackEventProcessingError("Callback event processing failed") from error

    async def _publish_completed_action(
        self,
        *,
        action: CallbackAction | None,
        result_id: UUID | None,
        event_id: UUID,
        organization_id: UUID,
        chat_id: int,
        message_id: int,
        catalog_status: str | None,
    ) -> None:
        if action is None or result_id is None:
            raise ValueError("Completed callback is missing its action result")

        if action is CallbackAction.SELECT_VARIANT:
            outcome_type = ProcessingOutcomeType.PROPOSAL_READY
            aggregate_id = result_id
            payload: dict[str, object] = {}
        elif action is CallbackAction.SHOW_EXISTING_ITEMS:
            outcome_type = ProcessingOutcomeType.PROPOSAL_READY
            aggregate_id = result_id
            payload = {}
        elif action is CallbackAction.ADD_NEW_ITEM:
            if catalog_status == "awaiting_confirmation":
                outcome_type = ProcessingOutcomeType.CATALOG_ITEM_CONFIRMATION
            elif catalog_status in (None, "awaiting_details"):
                outcome_type = ProcessingOutcomeType.CATALOG_ITEM_DETAILS_REQUIRED
            else:
                raise ValueError("Catalog item request is not awaiting user action")
            aggregate_id = result_id
            payload = {}
        elif action is CallbackAction.CONFIRM_NEW_ITEM:
            outcome_type = ProcessingOutcomeType.PROPOSAL_READY
            aggregate_id = result_id
            payload = {}
        elif action is CallbackAction.CANCEL_NEW_ITEM:
            outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
            aggregate_id = None
            payload = {
                "message": (
                    "🚫 **Catalog item creation cancelled**\n"
                    "No catalog or inventory changes were applied."
                )
            }
        elif action is CallbackAction.CONFIRM_PROPOSAL:
            outcome_type = ProcessingOutcomeType.TRANSACTION_APPLIED
            aggregate_id = result_id
            payload = {}
        elif action is CallbackAction.CANCEL_PROPOSAL:
            outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
            aggregate_id = None
            payload = {"message": "🚫 **Proposal cancelled**\nNo inventory changes were applied."}
        elif action is CallbackAction.REVERSE_TRANSACTION:
            outcome_type = ProcessingOutcomeType.REVERSAL_REASON_REQUIRED
            aggregate_id = result_id
            payload = {}
        elif action is CallbackAction.CONFIRM_REVERSAL:
            outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
            aggregate_id = None
            payload = {
                "message": (
                    "✅ **Transaction reversed**\n"
                    "The opposite inventory transaction was applied successfully."
                )
            }
        elif action is CallbackAction.CANCEL_REVERSAL:
            outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
            aggregate_id = None
            payload = {"message": "🚫 **Reversal cancelled**\nNo inventory changes were applied."}
        else:
            raise ValueError("Completed callback action is not supported")

        await self._outbox.enqueue(
            ProcessingOutcomeDraft(
                organization_id=organization_id,
                source_event_id=event_id,
                outcome_type=outcome_type,
                aggregate_id=aggregate_id,
                chat_id=chat_id,
                payload=payload,
            )
        )
        await self._message_editor.remove_inline_keyboard(
            chat_id=chat_id,
            message_id=message_id,
        )
