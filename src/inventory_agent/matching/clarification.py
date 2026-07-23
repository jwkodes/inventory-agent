"""Durable multi-turn clarification for unresolved candidate judgments."""

from typing import Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from inventory_agent.extraction.schema import ExtractedCommandLine
from inventory_agent.matching.judge import CandidateJudgeOutput
from inventory_agent.matching.models import InventoryCandidate


class MatchClarificationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    proposal_id: UUID
    proposal_line_id: UUID
    line: ExtractedCommandLine
    question: str
    accumulated_attributes: dict[str, str] = Field(default_factory=dict)
    clarification_replies: list[str] = Field(default_factory=list)
    candidates: list[InventoryCandidate]


class MatchClarificationRepository(Protocol):
    async def begin(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        chat_id: int,
    ) -> int:
        """Persist clarification requests for unresolved proposal lines."""

    async def find_pending(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        """Find the oldest question awaiting this user's reply in this chat."""

    async def get_view(self, *, request_id: UUID) -> MatchClarificationView:
        """Load the original line, candidates, attributes, and prior replies."""

    async def apply(
        self,
        *,
        request_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        user_reply: str,
        judgment: CandidateJudgeOutput,
    ) -> UUID:
        """Persist one judgment turn and return the parent proposal ID."""


class SupabaseMatchClarificationRepository:
    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rest_url = f"{supabase_url.rstrip('/')}/rest/v1/rpc"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def begin(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        chat_id: int,
    ) -> int:
        result = await self._call(
            "begin_match_clarifications",
            {
                "p_proposal_id": str(proposal_id),
                "p_actor_id": str(actor_id),
                "p_chat_id": chat_id,
            },
        )
        if not isinstance(result, int):
            raise ValueError("Supabase returned an invalid clarification count")
        return result

    async def find_pending(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        result = await self._call(
            "find_pending_match_clarification",
            {"p_actor_id": str(actor_id), "p_chat_id": chat_id},
        )
        if result is None:
            return None
        if not isinstance(result, str):
            raise ValueError("Supabase returned an invalid clarification request ID")
        return UUID(result)

    async def get_view(self, *, request_id: UUID) -> MatchClarificationView:
        result = await self._call(
            "get_match_clarification_view",
            {"p_request_id": str(request_id)},
        )
        return MatchClarificationView.model_validate(result)

    async def apply(
        self,
        *,
        request_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        user_reply: str,
        judgment: CandidateJudgeOutput,
    ) -> UUID:
        result = await self._call(
            "apply_match_clarification_judgment",
            {
                "p_request_id": str(request_id),
                "p_event_id": str(event_id),
                "p_actor_id": str(actor_id),
                "p_user_reply": user_reply,
                "p_action": judgment.action.value,
                "p_selected_candidate_id": (
                    str(judgment.selected_candidate_id)
                    if judgment.selected_candidate_id is not None
                    else None
                ),
                "p_question": judgment.question,
                "p_reason": judgment.reason,
                "p_resolved_attributes": {
                    attribute.key: attribute.value for attribute in judgment.resolved_attributes
                },
            },
        )
        if not isinstance(result, str):
            raise ValueError("Supabase returned an invalid clarification proposal ID")
        return UUID(result)

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
