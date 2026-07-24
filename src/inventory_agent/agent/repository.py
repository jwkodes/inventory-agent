"""Supabase persistence and read models for Telegram agent conversations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from inventory_agent.agent.models import TransactionRecord


class AgentConversationTurn(BaseModel):
    """One immutable model/tool-history segment with a compaction boundary."""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    source_event_id: UUID
    history: list[dict[str, object]] = Field(default_factory=list)
    estimated_tokens: int = Field(ge=1)
    created_at: datetime


class AgentConversation(BaseModel):
    """Durable model context and grounded IDs for one Telegram actor/chat."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    organization_id: UUID
    organization_user_id: UUID
    chat_id: int
    history: list[dict[str, object]] = Field(default_factory=list)
    allowed_variant_ids: list[UUID] = Field(default_factory=list)
    allowed_transaction_ids: list[UUID] = Field(default_factory=list)
    summary: str | None = None
    active_turns: list[AgentConversationTurn] = Field(default_factory=list)
    last_source_event_id: UUID | None = None
    last_reply_text: str | None = None
    last_proposal_id: UUID | None = None
    last_reversal_request_id: UUID | None = None
    last_reversal_reason: str | None = None
    last_response_id: str | None = None
    model_name: str | None = None


class VariantBalance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_variant_id: UUID
    on_hand: Decimal


class AgentConversationRepository(Protocol):
    async def load(
        self,
        *,
        organization_id: UUID,
        organization_user_id: UUID,
        chat_id: int,
    ) -> AgentConversation:
        """Load or create the durable conversation for an actor and Telegram chat."""

    async def save(
        self,
        *,
        conversation_id: UUID,
        source_event_id: UUID,
        organization_user_id: UUID,
        history: list[dict[str, object]],
        turn_history: list[dict[str, object]],
        estimated_tokens: int,
        allowed_variant_ids: set[UUID],
        allowed_transaction_ids: set[UUID],
        reply_text: str,
        proposal_id: UUID | None,
        reversal_request_id: UUID | None,
        reversal_reason: str | None,
        response_id: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> UUID:
        """Persist one completed agent turn and its replay metadata."""

    async def compact(
        self,
        *,
        conversation_id: UUID,
        organization_user_id: UUID,
        turn_ids: list[UUID],
        policy: str,
        summary: str | None,
    ) -> UUID:
        """Remove selected turns from active context while retaining their audit rows."""

    async def load_context_settings(
        self,
        *,
        organization_id: UUID,
    ) -> dict[str, object] | None:
        """Load the organization's complete context-setting override when configured."""

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
        """Add an authoritative callback result to agent-visible conversation history."""


class AgentReadRepository(Protocol):
    async def get_variant_balances(
        self,
        *,
        organization_id: UUID,
        location_id: UUID,
        variant_ids: list[UUID],
    ) -> dict[UUID, Decimal]:
        """Return current location balances for organization-owned variants."""

    async def read_transactions(
        self,
        *,
        organization_id: UUID,
        query: str | None,
        limit: int,
    ) -> list[TransactionRecord]:
        """Return recent organization-scoped transactions."""


class SupabaseAgentRepository:
    """Call security-definer agent persistence and read functions."""

    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rest_url = f"{supabase_url.rstrip('/')}/rest/v1/rpc"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def load(
        self,
        *,
        organization_id: UUID,
        organization_user_id: UUID,
        chat_id: int,
    ) -> AgentConversation:
        result = await self._call(
            "load_inventory_agent_conversation",
            {
                "p_organization_id": str(organization_id),
                "p_actor_id": str(organization_user_id),
                "p_chat_id": chat_id,
            },
        )
        return AgentConversation.model_validate(result)

    async def save(
        self,
        *,
        conversation_id: UUID,
        source_event_id: UUID,
        organization_user_id: UUID,
        history: list[dict[str, object]],
        turn_history: list[dict[str, object]],
        estimated_tokens: int,
        allowed_variant_ids: set[UUID],
        allowed_transaction_ids: set[UUID],
        reply_text: str,
        proposal_id: UUID | None,
        reversal_request_id: UUID | None,
        reversal_reason: str | None,
        response_id: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> UUID:
        result = await self._call(
            "save_inventory_agent_conversation_turn",
            {
                "p_conversation_id": str(conversation_id),
                "p_source_event_id": str(source_event_id),
                "p_actor_id": str(organization_user_id),
                "p_history": history,
                "p_turn_history": turn_history,
                "p_estimated_tokens": estimated_tokens,
                "p_allowed_variant_ids": [
                    str(value) for value in sorted(allowed_variant_ids, key=str)
                ],
                "p_allowed_transaction_ids": [
                    str(value) for value in sorted(allowed_transaction_ids, key=str)
                ],
                "p_reply_text": reply_text,
                "p_proposal_id": str(proposal_id) if proposal_id else None,
                "p_reversal_request_id": (
                    str(reversal_request_id) if reversal_request_id else None
                ),
                "p_reversal_reason": reversal_reason,
                "p_response_id": response_id,
                "p_model_name": model_name,
                "p_input_tokens": input_tokens,
                "p_output_tokens": output_tokens,
                "p_total_tokens": total_tokens,
            },
        )
        if not isinstance(result, str):
            raise ValueError("Supabase returned an invalid agent conversation ID")
        return UUID(result)

    async def compact(
        self,
        *,
        conversation_id: UUID,
        organization_user_id: UUID,
        turn_ids: list[UUID],
        policy: str,
        summary: str | None,
    ) -> UUID:
        result = await self._call(
            "compact_inventory_agent_conversation",
            {
                "p_conversation_id": str(conversation_id),
                "p_actor_id": str(organization_user_id),
                "p_turn_ids": [str(turn_id) for turn_id in turn_ids],
                "p_policy": policy,
                "p_summary": summary,
            },
        )
        if not isinstance(result, str):
            raise ValueError("Supabase returned an invalid compacted conversation ID")
        return UUID(result)

    async def load_context_settings(
        self,
        *,
        organization_id: UUID,
    ) -> dict[str, object] | None:
        result = await self._call(
            "load_organization_agent_context_settings",
            {"p_organization_id": str(organization_id)},
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise ValueError("Supabase returned invalid organization context settings")
        return result

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
        result = await self._call(
            "record_inventory_agent_callback_outcome",
            {
                "p_organization_id": str(organization_id),
                "p_actor_id": str(organization_user_id),
                "p_chat_id": chat_id,
                "p_source_event_id": str(source_event_id),
                "p_action": action,
                "p_result_id": str(result_id),
            },
        )
        if result is None:
            return None
        if not isinstance(result, str):
            raise ValueError("Supabase returned an invalid callback conversation ID")
        return UUID(result)

    async def get_variant_balances(
        self,
        *,
        organization_id: UUID,
        location_id: UUID,
        variant_ids: list[UUID],
    ) -> dict[UUID, Decimal]:
        if not variant_ids:
            return {}
        result = await self._call(
            "get_inventory_agent_variant_balances",
            {
                "p_organization_id": str(organization_id),
                "p_location_id": str(location_id),
                "p_variant_ids": [str(value) for value in variant_ids],
            },
        )
        if not isinstance(result, list):
            raise ValueError("Supabase returned invalid agent inventory balances")
        rows = [VariantBalance.model_validate(row) for row in result]
        return {row.item_variant_id: row.on_hand for row in rows}

    async def read_transactions(
        self,
        *,
        organization_id: UUID,
        query: str | None,
        limit: int,
    ) -> list[TransactionRecord]:
        result = await self._call(
            "read_inventory_agent_transactions",
            {
                "p_organization_id": str(organization_id),
                "p_query": query,
                "p_limit": limit,
            },
        )
        if not isinstance(result, list):
            raise ValueError("Supabase returned invalid agent transactions")
        return [TransactionRecord.model_validate(row) for row in result]

    async def _call(self, function_name: str, body: dict[str, object]) -> object:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(f"/{function_name}", json=body)
        response.raise_for_status()
        return response.json()
