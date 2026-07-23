"""Process durable Telegram callback events and refresh their source messages."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from inventory_agent.processing.models import TelegramCallbackEventContext
from inventory_agent.telegram.callback_dispatcher import (
    CallbackOutcome,
    CallbackOutcomeStatus,
)
from inventory_agent.telegram.callbacks import CallbackAction
from inventory_agent.telegram.confirmation import (
    ConfirmationMessage,
    ProposalConfirmationView,
    render_applied_transaction,
    render_proposal_confirmation,
    render_reversal_reason_prompt,
)


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


class ProposalViewRepository(Protocol):
    async def get_proposal_view(self, proposal_id: UUID) -> ProposalConfirmationView:
        """Load current proposal lines after a variant selection."""


class TelegramMessageEditor(Protocol):
    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Edit the message that originated a callback."""


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
        proposal_views: ProposalViewRepository,
        message_editor: TelegramMessageEditor,
    ) -> None:
        self._events = events
        self._dispatcher = dispatcher
        self._proposal_views = proposal_views
        self._message_editor = message_editor

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
                await self._render_completed_action(
                    action=outcome.action,
                    result_id=outcome.result_id,
                    chat_id=context.chat_id,
                    message_id=context.telegram_message_id,
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

    async def _render_completed_action(
        self,
        *,
        action: CallbackAction | None,
        result_id: UUID | None,
        chat_id: int,
        message_id: int,
    ) -> None:
        if action is None or result_id is None:
            raise ValueError("Completed callback is missing its action result")

        if action is CallbackAction.SELECT_VARIANT:
            view = await self._proposal_views.get_proposal_view(result_id)
            message = render_proposal_confirmation(
                proposal_id=view.proposal_id,
                intent_label=_intent_label(view.intent),
                lines=view.lines,
            )
            keyboard = [
                [button.model_dump(mode="json") for button in row]
                for row in message.inline_keyboard
            ]
            text = message.text
        elif action is CallbackAction.CONFIRM_PROPOSAL:
            message = render_applied_transaction(result_id)
            text = message.text
            keyboard = _keyboard(message)
        elif action is CallbackAction.CANCEL_PROPOSAL:
            text = "Proposal cancelled."
            keyboard = None
        elif action is CallbackAction.REVERSE_TRANSACTION:
            message = render_reversal_reason_prompt(result_id)
            text = message.text
            keyboard = _keyboard(message)
        elif action is CallbackAction.CONFIRM_REVERSAL:
            text = "Transaction reversed."
            keyboard = None
        elif action is CallbackAction.CANCEL_REVERSAL:
            text = "Reversal cancelled."
            keyboard = None
        else:
            raise ValueError("Completed callback action is not supported")

        await self._message_editor.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            inline_keyboard=keyboard,
        )


def _intent_label(intent: str) -> str:
    labels = {
        "receive_stock": "stock receipt",
        "issue_stock": "stock issue",
        "adjust_stock": "stock adjustment",
    }
    return labels.get(intent, intent.replace("_", " "))


def _keyboard(message: ConfirmationMessage) -> list[list[dict[str, str]]]:
    return [[button.model_dump(mode="json") for button in row] for row in message.inline_keyboard]
