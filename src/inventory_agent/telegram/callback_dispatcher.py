"""Acknowledge and dispatch authenticated Telegram callback actions."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.catalog.repository import CatalogItemCreationRepository
from inventory_agent.proposals.actions import ProposalActionRepository
from inventory_agent.reversals.repository import ReversalRepository
from inventory_agent.telegram.callbacks import CallbackAction, decode_callback


class CallbackAnswerer(Protocol):
    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None: ...


class CallbackOutcomeStatus(StrEnum):
    COMPLETED = "completed"
    INVALID = "invalid"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CallbackOutcome:
    status: CallbackOutcomeStatus
    action: CallbackAction | None
    result_id: UUID | None
    message: str


class TelegramCallbackDispatcher:
    def __init__(
        self,
        *,
        answerer: CallbackAnswerer,
        repository: ProposalActionRepository,
        reversals: ReversalRepository,
        catalog: CatalogItemCreationRepository,
    ) -> None:
        self._answerer = answerer
        self._repository = repository
        self._reversals = reversals
        self._catalog = catalog

    async def dispatch(
        self,
        *,
        callback_query_id: str,
        callback_data: str,
        actor_id: UUID,
        chat_id: int,
    ) -> CallbackOutcome:
        """Acknowledge first, then execute exactly one decoded database action."""

        try:
            command = decode_callback(callback_data)
        except ValueError:
            await self._try_answer(
                callback_query_id=callback_query_id,
                text="This action is invalid or expired.",
                show_alert=True,
            )
            return CallbackOutcome(
                CallbackOutcomeStatus.INVALID,
                None,
                None,
                "Malformed callback data",
            )

        await self._try_answer(callback_query_id=callback_query_id)

        try:
            if command.action is CallbackAction.SELECT_VARIANT:
                if command.choice_id is None:
                    raise ValueError("Selection is missing a variant")
                result_id = await self._repository.select_variant(
                    line_id=command.target_id,
                    variant_id=command.choice_id,
                    actor_id=actor_id,
                )
                message = "Item selected"
            elif command.action is CallbackAction.CONFIRM_PROPOSAL:
                result_id = await self._repository.confirm(
                    proposal_id=command.target_id,
                    actor_id=actor_id,
                )
                message = "Inventory updated"
            elif command.action is CallbackAction.CANCEL_PROPOSAL:
                result_id = await self._repository.cancel(
                    proposal_id=command.target_id,
                    actor_id=actor_id,
                )
                message = "Proposal cancelled"
            elif command.action is CallbackAction.ADD_NEW_ITEM:
                result_id = await self._catalog.begin(
                    line_id=command.target_id,
                    actor_id=actor_id,
                    chat_id=chat_id,
                )
                message = "Catalog item details required"
            elif command.action is CallbackAction.SHOW_EXISTING_ITEMS:
                result_id = await self._catalog.show_existing(
                    line_id=command.target_id,
                    actor_id=actor_id,
                )
                message = "Existing catalog candidates ready"
            elif command.action is CallbackAction.CONFIRM_NEW_ITEM:
                result_id = await self._catalog.confirm(
                    request_id=command.target_id,
                    actor_id=actor_id,
                )
                message = "Catalog item created"
            elif command.action is CallbackAction.CANCEL_NEW_ITEM:
                result_id = await self._catalog.cancel(
                    request_id=command.target_id,
                    actor_id=actor_id,
                )
                message = "Catalog item creation cancelled"
            elif command.action is CallbackAction.REVERSE_TRANSACTION:
                result_id = await self._reversals.begin(
                    transaction_id=command.target_id,
                    actor_id=actor_id,
                    chat_id=chat_id,
                )
                message = "Reversal reason required"
            elif command.action is CallbackAction.CONFIRM_REVERSAL:
                result_id = await self._reversals.confirm(
                    request_id=command.target_id,
                    actor_id=actor_id,
                )
                message = "Transaction reversed"
            elif command.action is CallbackAction.CANCEL_REVERSAL:
                result_id = await self._reversals.cancel(
                    request_id=command.target_id,
                    actor_id=actor_id,
                )
                message = "Reversal cancelled"
            else:
                raise ValueError("Unsupported callback action")
        except (ValueError, RuntimeError, httpx.HTTPError) as error:
            return CallbackOutcome(
                CallbackOutcomeStatus.FAILED,
                command.action,
                None,
                str(error),
            )

        return CallbackOutcome(
            CallbackOutcomeStatus.COMPLETED,
            command.action,
            result_id,
            message,
        )

    async def _try_answer(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        """Acknowledgement is best-effort; durable database work must remain retryable."""

        try:
            await self._answerer.answer_callback_query(
                callback_query_id=callback_query_id,
                text=text,
                show_alert=show_alert,
            )
        except (RuntimeError, httpx.HTTPError):
            return
