"""Deliver durable processing outcomes through Telegram."""

import logging
from time import perf_counter
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

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
    render_reversal_applied,
    render_reversal_confirmation,
    render_reversal_reason_prompt,
)

logger = logging.getLogger(__name__)


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
        display_timezone: str = "Asia/Singapore",
    ) -> None:
        self._repository = repository
        self._sender = sender
        self._retry_delay_seconds = retry_delay_seconds
        self._display_timezone = ZoneInfo(display_timezone)

    async def deliver_one(self, outbox_id: UUID | None = None) -> OutboxDeliveryResult:
        """Claim and deliver at most one due outcome."""

        total_started = perf_counter()
        claim_started = perf_counter()
        outcome = await self._repository.claim(outbox_id)
        if outcome is None:
            return OutboxDeliveryResult(status=OutboxDeliveryStatus.IDLE)
        _log_runtime(
            component="outbox_claim",
            started=claim_started,
            outbox_id=outcome.outbox_id,
        )

        try:
            render_started = perf_counter()
            if outcome.outcome_type is ProcessingOutcomeType.PROPOSAL_READY:
                if outcome.aggregate_id is None:
                    raise ValueError("Proposal-ready outcome is missing its proposal ID")
                view = await self._repository.get_proposal_view(outcome.aggregate_id)
                message = render_proposal_confirmation(
                    proposal_id=view.proposal_id,
                    intent_label=_intent_label(view.intent),
                    lines=view.lines,
                )
                text = _with_agent_reply(message.text, outcome.payload)
                if reversal_transaction_id := _payload_uuid(
                    outcome.payload,
                    "reversal_transaction_id",
                ):
                    reversal_transaction = await self._repository.get_applied_transaction(
                        organization_id=outcome.organization_id,
                        transaction_id=reversal_transaction_id,
                    )
                    reversal_notice = render_reversal_applied(
                        transaction_id=reversal_transaction.transaction_id,
                        applied_at=reversal_transaction.applied_at,
                        display_timezone=self._display_timezone,
                    )
                    text = f"{reversal_notice}\n\n{text}"
                keyboard = [
                    [button.model_dump(mode="json") for button in row]
                    for row in message.inline_keyboard
                ]
            elif outcome.outcome_type is ProcessingOutcomeType.TRANSACTION_APPLIED:
                if outcome.aggregate_id is None:
                    raise ValueError("Applied outcome is missing its transaction ID")
                transaction = await self._repository.get_applied_transaction(
                    organization_id=outcome.organization_id,
                    transaction_id=outcome.aggregate_id,
                )
                message = render_applied_transaction(
                    outcome.aggregate_id,
                    transaction_type=transaction.transaction_type,
                    applied_at=transaction.applied_at,
                    display_timezone=self._display_timezone,
                )
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
                if (
                    outcome.outcome_type is ProcessingOutcomeType.CATALOG_ITEM_DETAILS_REQUIRED
                    and catalog_view.status == "awaiting_details"
                ):
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
                original_transaction = await self._repository.get_reversal_original_transaction(
                    organization_id=outcome.organization_id,
                    request_id=outcome.aggregate_id,
                )
                message = render_reversal_confirmation(
                    request_id=outcome.aggregate_id,
                    original_transaction_id=original_transaction.transaction_id,
                    reason=reason.strip(),
                    original_transaction_applied_at=original_transaction.applied_at,
                    display_timezone=self._display_timezone,
                )
                text = _with_agent_reply(message.text, outcome.payload)
                keyboard = [
                    [button.model_dump(mode="json") for button in row]
                    for row in message.inline_keyboard
                ]
            elif outcome.outcome_type is ProcessingOutcomeType.CALLBACK_NOTICE and (
                reversal_transaction_id := _payload_uuid(outcome.payload, "transaction_id")
            ):
                reversal_transaction = await self._repository.get_applied_transaction(
                    organization_id=outcome.organization_id,
                    transaction_id=reversal_transaction_id,
                )
                text = render_reversal_applied(
                    transaction_id=reversal_transaction.transaction_id,
                    applied_at=reversal_transaction.applied_at,
                    display_timezone=self._display_timezone,
                )
                keyboard = None
            else:
                payload_message = outcome.payload.get("message")
                if not isinstance(payload_message, str) or not payload_message.strip():
                    raise ValueError("Text outcome is missing its message")
                text = _style_plain_outcome(outcome.outcome_type, payload_message)
                keyboard = None

            text = _with_dev_identity(text, outcome.payload)
            _log_runtime(
                component="telegram_message_render",
                started=render_started,
                outbox_id=outcome.outbox_id,
                outcome_type=outcome.outcome_type,
            )
            send_started = perf_counter()
            telegram_message_id = await self._sender.send_message(
                chat_id=outcome.chat_id,
                text=text,
                inline_keyboard=keyboard,
            )
            _log_runtime(
                component="telegram_api_send",
                started=send_started,
                outbox_id=outcome.outbox_id,
            )
            finish_started = perf_counter()
            completion = await self._repository.finish(
                outbox_id=outcome.outbox_id,
                success=True,
            )
            if completion is not OutboxCompletionStatus.SENT:
                raise OutboxDeliveryError("Sent Telegram outcome could not be completed")
            _log_runtime(
                component="outbox_finish",
                started=finish_started,
                outbox_id=outcome.outbox_id,
            )
            _log_runtime(
                component="outbox_delivery_total",
                started=total_started,
                outbox_id=outcome.outbox_id,
                status=OutboxDeliveryStatus.SENT,
            )
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
            _log_runtime(
                component="outbox_delivery_total",
                started=total_started,
                outbox_id=outcome.outbox_id,
                status=status,
            )
            return OutboxDeliveryResult(status=status, outbox_id=outcome.outbox_id)


def _intent_label(intent: str) -> str:
    labels = {
        "receive_stock": "stock receipt",
        "issue_stock": "stock issue",
        "adjust_stock": "stock adjustment",
    }
    return labels.get(intent, intent.replace("_", " "))


def _log_runtime(
    *,
    component: str,
    started: float,
    outbox_id: UUID,
    **fields: object,
) -> None:
    details = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    logger.info(
        "component_runtime component=%s duration_ms=%.2f outbox_id=%s%s",
        component,
        (perf_counter() - started) * 1000,
        outbox_id,
        f" {details}" if details else "",
    )


def _with_agent_reply(rendered_text: str, payload: dict[str, object]) -> str:
    agent_reply = payload.get("agent_reply")
    if not isinstance(agent_reply, str) or not agent_reply.strip():
        return rendered_text
    return f"{rendered_text}\n\n💬 **Agent note**\n{agent_reply.strip()}"


def _style_plain_outcome(outcome_type: ProcessingOutcomeType, message: str) -> str:
    """Add a deterministic status heading to legacy plain outcomes."""

    stripped = message.strip()
    if stripped.startswith(("✅", "⏳", "🚫", "❓", "⚠️", "🔎", "📝", "🤖")):
        return stripped
    if outcome_type is ProcessingOutcomeType.CLARIFICATION_REQUIRED:
        return f"❓ **More information needed**\n{stripped}"
    if outcome_type is ProcessingOutcomeType.UNSUPPORTED_COMMAND:
        return f"🤖 **Inventory assistant**\n{stripped}"
    return stripped


def _with_dev_identity(rendered_text: str, payload: dict[str, object]) -> str:
    simulation = payload.get("_dev_simulation")
    if not isinstance(simulation, dict):
        return rendered_text
    display_name = simulation.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        return rendered_text
    return f"🧪 **Simulating {display_name.strip()}**\n\n{rendered_text}"


def _payload_uuid(payload: dict[str, object], key: str) -> UUID | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Telegram outcome has an invalid {key}")
    return UUID(value)
