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

_CONTEXT_LIFECYCLE_ACTIONS = {
    CallbackAction.CONFIRM_PROPOSAL,
    CallbackAction.CANCEL_PROPOSAL,
    CallbackAction.CONFIRM_NEW_ITEM,
    CallbackAction.CANCEL_NEW_ITEM,
    CallbackAction.CONFIRM_CATALOG_BATCH,
    CallbackAction.CANCEL_CATALOG_BATCH,
    CallbackAction.CONFIRM_REVERSAL,
    CallbackAction.CANCEL_REVERSAL,
}


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


class AgentCallbackOutcomeRecorder(Protocol):
    async def record_callback_outcome(
        self,
        *,
        organization_id: UUID,
        organization_user_id: UUID,
        chat_id: int,
        source_event_id: UUID,
        action: str,
        result_id: UUID,
    ) -> UUID | None:
        """Add a deterministic callback result to agent-visible context."""


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
        conversation_recorder: AgentCallbackOutcomeRecorder | None = None,
    ) -> None:
        self._events = events
        self._dispatcher = dispatcher
        self._message_editor = message_editor
        self._outbox = outbox
        self._conversation_recorder = conversation_recorder

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
                await self._outbox.enqueue(
                    ProcessingOutcomeDraft(
                        organization_id=context.organization_id,
                        source_event_id=context.event_id,
                        outcome_type=ProcessingOutcomeType.CALLBACK_NOTICE,
                        aggregate_id=None,
                        chat_id=context.chat_id,
                        payload={"message": (f"⚠️ **Action not completed**\n{outcome.message}")},
                    )
                )

            if outcome.status is CallbackOutcomeStatus.COMPLETED:
                await self._publish_completed_action(
                    action=outcome.action,
                    result_id=outcome.result_id,
                    event_id=context.event_id,
                    organization_id=context.organization_id,
                    actor_id=context.organization_user_id,
                    chat_id=context.chat_id,
                    message_id=context.telegram_message_id,
                    callback_message=outcome.message,
                    catalog_status=outcome.catalog_status,
                    catalog_batch_status=outcome.catalog_batch_status,
                    replacement_proposal_id=outcome.replacement_proposal_id,
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
        actor_id: UUID,
        chat_id: int,
        message_id: int,
        callback_message: str,
        catalog_status: str | None,
        catalog_batch_status: str | None,
        replacement_proposal_id: UUID | None,
    ) -> None:
        if action is None or result_id is None:
            raise ValueError("Completed callback is missing its action result")

        if action in {
            CallbackAction.SELECT_VARIANT,
            CallbackAction.MARK_NEW_ITEM,
            CallbackAction.IGNORE_PROPOSAL_LINE,
            CallbackAction.SHOW_EXISTING_ITEMS,
        }:
            outcome_type = ProcessingOutcomeType.PROPOSAL_READY
            aggregate_id = result_id
            payload: dict[str, object] = {}
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
            if catalog_status == "awaiting_details":
                outcome_type = ProcessingOutcomeType.CATALOG_ITEM_DETAILS_REQUIRED
            else:
                outcome_type = ProcessingOutcomeType.PROPOSAL_READY
            aggregate_id = result_id
            payload = {}
        elif action is CallbackAction.CANCEL_NEW_ITEM:
            outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
            aggregate_id = None
            payload = {"message": "🚫 **Catalog item creation cancelled**"}
        elif action is CallbackAction.ADD_ALL_NEW_ITEMS:
            if catalog_batch_status == "awaiting_confirmation":
                outcome_type = ProcessingOutcomeType.CATALOG_BATCH_CONFIRMATION
            elif catalog_batch_status in (None, "awaiting_details"):
                outcome_type = ProcessingOutcomeType.CATALOG_BATCH_DETAILS_REQUIRED
            else:
                raise ValueError("Catalog batch is not awaiting user action")
            aggregate_id = result_id
            payload = {}
        elif action is CallbackAction.CONFIRM_CATALOG_BATCH:
            if catalog_batch_status == "awaiting_details":
                outcome_type = ProcessingOutcomeType.CATALOG_BATCH_DETAILS_REQUIRED
                aggregate_id = result_id
                payload = {"message": callback_message}
            else:
                outcome_type = ProcessingOutcomeType.TRANSACTION_APPLIED
                aggregate_id = result_id
                payload = {}
        elif action is CallbackAction.CANCEL_CATALOG_BATCH:
            outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
            aggregate_id = None
            payload = {"message": "🚫 **Catalog batch cancelled**"}
        elif action is CallbackAction.CONFIRM_PROPOSAL:
            outcome_type = ProcessingOutcomeType.TRANSACTION_APPLIED
            aggregate_id = result_id
            payload = {}
        elif action is CallbackAction.CANCEL_PROPOSAL:
            outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
            aggregate_id = None
            payload = {"message": "🚫 **Proposal cancelled**"}
        elif action is CallbackAction.REVERSE_TRANSACTION:
            outcome_type = ProcessingOutcomeType.REVERSAL_REASON_REQUIRED
            aggregate_id = result_id
            payload = {}
        elif action is CallbackAction.CONFIRM_REVERSAL:
            if replacement_proposal_id is not None:
                outcome_type = ProcessingOutcomeType.PROPOSAL_READY
                aggregate_id = replacement_proposal_id
                payload = {"reversal_transaction_id": str(result_id)}
            else:
                outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
                aggregate_id = None
                payload = {
                    "message": (
                        "✅ **Transaction reversed**\nThe original stock movement was reversed."
                    ),
                    "transaction_id": str(result_id),
                }
        elif action is CallbackAction.CANCEL_REVERSAL:
            outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
            aggregate_id = None
            payload = {"message": "🚫 **Reversal cancelled**"}
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
        conflict_reopened = (
            action is CallbackAction.CONFIRM_NEW_ITEM and catalog_status == "awaiting_details"
        ) or (
            action is CallbackAction.CONFIRM_CATALOG_BATCH
            and catalog_batch_status == "awaiting_details"
        )
        if (
            self._conversation_recorder is not None
            and action in _CONTEXT_LIFECYCLE_ACTIONS
            and not conflict_reopened
        ):
            await self._conversation_recorder.record_callback_outcome(
                organization_id=organization_id,
                organization_user_id=actor_id,
                chat_id=chat_id,
                source_event_id=event_id,
                action=action.name.casefold(),
                result_id=result_id,
            )
        await self._message_editor.remove_inline_keyboard(
            chat_id=chat_id,
            message_id=message_id,
        )
