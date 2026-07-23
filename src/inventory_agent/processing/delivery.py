"""Deliver durable processing outcomes through Telegram."""

from typing import Protocol
from uuid import UUID

from inventory_agent.processing.models import (
    OutboxCompletionStatus,
    OutboxDeliveryResult,
    OutboxDeliveryStatus,
    ProcessingOutcomeType,
)
from inventory_agent.processing.repository import ProcessingOutboxDeliveryRepository
from inventory_agent.telegram.confirmation import (
    render_applied_transaction,
    render_catalog_item_confirmation,
    render_catalog_item_details_prompt,
    render_proposal_confirmation,
    render_reversal_confirmation,
    render_reversal_reason_prompt,
)


class TelegramMessageSender(Protocol):
    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        """Send one Telegram message and return its provider message ID."""


class OutboxDeliveryError(RuntimeError):
    """Delivery state could not be made internally consistent."""


class TelegramOutboxDeliveryWorker:
    def __init__(
        self,
        *,
        repository: ProcessingOutboxDeliveryRepository,
        sender: TelegramMessageSender,
        retry_delay_seconds: int = 30,
    ) -> None:
        self._repository = repository
        self._sender = sender
        self._retry_delay_seconds = retry_delay_seconds

    async def deliver_one(self, outbox_id: UUID | None = None) -> OutboxDeliveryResult:
        """Claim and deliver at most one due outcome."""

        outcome = await self._repository.claim(outbox_id)
        if outcome is None:
            return OutboxDeliveryResult(status=OutboxDeliveryStatus.IDLE)

        try:
            if outcome.outcome_type is ProcessingOutcomeType.PROPOSAL_READY:
                if outcome.aggregate_id is None:
                    raise ValueError("Proposal-ready outcome is missing its proposal ID")
                view = await self._repository.get_proposal_view(outcome.aggregate_id)
                message = render_proposal_confirmation(
                    proposal_id=view.proposal_id,
                    intent_label=_intent_label(view.intent),
                    lines=view.lines,
                )
                text = message.text
                keyboard = [
                    [button.model_dump(mode="json") for button in row]
                    for row in message.inline_keyboard
                ]
            elif outcome.outcome_type is ProcessingOutcomeType.TRANSACTION_APPLIED:
                if outcome.aggregate_id is None:
                    raise ValueError("Applied outcome is missing its transaction ID")
                message = render_applied_transaction(outcome.aggregate_id)
                text = message.text
                keyboard = [
                    [button.model_dump(mode="json") for button in row]
                    for row in message.inline_keyboard
                ]
            elif outcome.outcome_type in {
                ProcessingOutcomeType.CATALOG_ITEM_DETAILS_REQUIRED,
                ProcessingOutcomeType.CATALOG_ITEM_CONFIRMATION,
            }:
                if outcome.aggregate_id is None:
                    raise ValueError("Catalog outcome is missing its request ID")
                catalog_view = await self._repository.get_catalog_item_creation_view(
                    outcome.aggregate_id
                )
                if outcome.outcome_type is ProcessingOutcomeType.CATALOG_ITEM_DETAILS_REQUIRED:
                    message = render_catalog_item_details_prompt(catalog_view)
                else:
                    message = render_catalog_item_confirmation(catalog_view)
                text = message.text
                keyboard = [
                    [button.model_dump(mode="json") for button in row]
                    for row in message.inline_keyboard
                ]
            elif outcome.outcome_type is ProcessingOutcomeType.REVERSAL_REASON_REQUIRED:
                if outcome.aggregate_id is None:
                    raise ValueError("Reversal reason outcome is missing its request ID")
                message = render_reversal_reason_prompt(outcome.aggregate_id)
                text = message.text
                keyboard = [
                    [button.model_dump(mode="json") for button in row]
                    for row in message.inline_keyboard
                ]
            elif outcome.outcome_type is ProcessingOutcomeType.REVERSAL_CONFIRMATION:
                if outcome.aggregate_id is None:
                    raise ValueError("Reversal outcome is missing its request ID")
                reason = outcome.payload.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("Reversal outcome is missing its reason")
                message = render_reversal_confirmation(
                    request_id=outcome.aggregate_id,
                    reason=reason.strip(),
                )
                text = message.text
                keyboard = [
                    [button.model_dump(mode="json") for button in row]
                    for row in message.inline_keyboard
                ]
            else:
                payload_message = outcome.payload.get("message")
                if not isinstance(payload_message, str) or not payload_message.strip():
                    raise ValueError("Text outcome is missing its message")
                text = payload_message
                keyboard = None

            telegram_message_id = await self._sender.send_message(
                chat_id=outcome.chat_id,
                text=text,
                inline_keyboard=keyboard,
            )
            completion = await self._repository.finish(
                outbox_id=outcome.outbox_id,
                success=True,
            )
            if completion is not OutboxCompletionStatus.SENT:
                raise OutboxDeliveryError("Sent Telegram outcome could not be completed")
            return OutboxDeliveryResult(
                status=OutboxDeliveryStatus.SENT,
                outbox_id=outcome.outbox_id,
                telegram_message_id=telegram_message_id,
            )
        except Exception as error:
            if isinstance(error, OutboxDeliveryError):
                raise
            completion = await self._repository.finish(
                outbox_id=outcome.outbox_id,
                success=False,
                error_message=f"{type(error).__name__}: Telegram delivery failed",
                retry_delay_seconds=self._retry_delay_seconds,
            )
            if completion is OutboxCompletionStatus.PENDING:
                status = OutboxDeliveryStatus.RETRY_SCHEDULED
            elif completion is OutboxCompletionStatus.FAILED:
                status = OutboxDeliveryStatus.DEAD_LETTERED
            else:
                raise OutboxDeliveryError(
                    "Failed Telegram outcome could not be rescheduled"
                ) from error
            return OutboxDeliveryResult(status=status, outbox_id=outcome.outbox_id)


def _intent_label(intent: str) -> str:
    labels = {
        "receive_stock": "stock receipt",
        "issue_stock": "stock issue",
        "adjust_stock": "stock adjustment",
    }
    return labels.get(intent, intent.replace("_", " "))
