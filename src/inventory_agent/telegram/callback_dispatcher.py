"""Acknowledge and dispatch authenticated Telegram callback actions."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.proposals.actions import ProposalActionRepository
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
    NEEDS_FOLLOW_UP = "needs_follow_up"


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
    ) -> None:
        self._answerer = answerer
        self._repository = repository

    async def dispatch(
        self,
        *,
        callback_query_id: str,
        callback_data: str,
        actor_id: UUID,
    ) -> CallbackOutcome:
        """Acknowledge first, then execute exactly one decoded database action."""

        try:
            command = decode_callback(callback_data)
        except ValueError:
            await self._answerer.answer_callback_query(
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

        await self._answerer.answer_callback_query(callback_query_id=callback_query_id)

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
            else:
                return CallbackOutcome(
                    CallbackOutcomeStatus.NEEDS_FOLLOW_UP,
                    command.action,
                    None,
                    "A reversal reason must be collected before reversal",
                )
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
