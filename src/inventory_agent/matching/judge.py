"""Constrained LLM judgment of already-retrieved inventory candidates."""

import json
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from openai import AsyncOpenAI
from openai.types.shared import ReasoningEffort
from pydantic import BaseModel, ConfigDict, Field

from inventory_agent.extraction.schema import ExtractedAttribute, ExtractedCommandLine
from inventory_agent.matching.models import InventoryCandidate

PROMPT_VERSION = "inventory-candidate-judge-v1"
INSTRUCTIONS = """You judge whether an inventory mention matches one of a small set of
catalog candidates. The candidates were retrieved by the application. You may select only
an item_variant_id present in CANDIDATES. Never invent a database ID or catalog fact.

Return exactly one action:
- SELECT when one candidate is compatible with the user's full meaning.
- ASK_USER when one missing fact would distinguish two or more plausible variants. Ask one
  short, natural question, such as "Which colour is it?"
- NO_MATCH when every offered candidate contradicts an explicit identity detail.

Treat explicit model, edition, generation, version, colour/color, size, formulation, and
strength details as likely variant identity constraints. A contradiction makes a candidate
incompatible. If the user states such a detail but the candidate has no supporting name,
SKU, or attribute evidence, do not silently ignore it. Expiry, batch, lot, and serial facts
normally describe a stock instance rather than catalog identity and should not reject an
otherwise compatible catalog variant.

ATTRIBUTE_MATCHING_ROLES may contain company configuration:
- discriminator: identity constraint; contradictions reject and missing values may require
  a question.
- supporting: useful evidence but not necessarily identity.
- operational: belongs to the receipt/issue instance, not catalog identity.
- ignored: do not use for matching.
Company configuration overrides the general defaults above.

Use the complete original wording, extracted attributes, and all prior clarification replies.
Do not select based only on semantic similarity. If action is SELECT, selected_candidate_id
must be the offered UUID. If action is ASK_USER, question must be the one missing fact needed.
If action is NO_MATCH, explain the explicit incompatibility briefly. Copy any facts learned
from clarification replies into resolved_attributes without inventing values.
"""


class CandidateJudgeAction(StrEnum):
    SELECT = "SELECT"
    ASK_USER = "ASK_USER"
    NO_MATCH = "NO_MATCH"


class CandidateJudgeOutput(BaseModel):
    """Strict model response; application code still validates cross-field invariants."""

    model_config = ConfigDict(extra="forbid")

    action: CandidateJudgeAction
    selected_candidate_id: UUID | None
    question: str | None
    reason: str
    resolved_attributes: list[ExtractedAttribute] = Field(default_factory=list)


class CandidateJudge(Protocol):
    async def judge(
        self,
        *,
        line: ExtractedCommandLine,
        candidates: list[InventoryCandidate],
        clarification_replies: list[str] | None = None,
        accumulated_attributes: dict[str, str] | None = None,
    ) -> CandidateJudgeOutput:
        """Judge only the supplied organization-scoped candidates."""


class CandidateJudgmentError(RuntimeError):
    """The judge returned an unusable or unsafe decision."""


class OpenAICandidateJudge:
    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        model: str,
        reasoning_effort: ReasoningEffort = "none",
    ) -> None:
        self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort

    async def judge(
        self,
        *,
        line: ExtractedCommandLine,
        candidates: list[InventoryCandidate],
        clarification_replies: list[str] | None = None,
        accumulated_attributes: dict[str, str] | None = None,
    ) -> CandidateJudgeOutput:
        if not candidates:
            raise ValueError("candidate judging requires at least one candidate")

        candidate_ids = {candidate.item_variant_id for candidate in candidates}
        roles: dict[str, str] = {}
        candidate_payload = []
        for candidate in candidates:
            roles.update(candidate.attribute_matching_roles)
            candidate_payload.append(
                {
                    "item_variant_id": str(candidate.item_variant_id),
                    "item_name": candidate.item_name,
                    "variant_name": candidate.variant_name,
                    "sku": candidate.sku,
                    "item_attributes": candidate.item_attributes,
                    "variant_attributes": candidate.variant_attributes,
                    "retrieval_score": str(candidate.match_score),
                }
            )

        payload = {
            "ORIGINAL_LINE": line.model_dump(mode="json"),
            "ACCUMULATED_ATTRIBUTES": accumulated_attributes or {},
            "PRIOR_CLARIFICATION_REPLIES": clarification_replies or [],
            "ATTRIBUTE_MATCHING_ROLES": roles,
            "CANDIDATES": candidate_payload,
        }
        response = await self._client.responses.parse(
            model=self._model,
            reasoning={"effort": self._reasoning_effort},
            instructions=INSTRUCTIONS,
            input=json.dumps(payload, ensure_ascii=False),
            text_format=CandidateJudgeOutput,
            store=False,
        )
        judgment = response.output_parsed
        if judgment is None:
            raise CandidateJudgmentError("OpenAI response did not contain a candidate judgment")
        _validate_judgment(judgment, candidate_ids)
        return judgment


def _validate_judgment(
    judgment: CandidateJudgeOutput,
    candidate_ids: set[UUID],
) -> None:
    if judgment.action is CandidateJudgeAction.SELECT:
        if judgment.selected_candidate_id not in candidate_ids:
            raise CandidateJudgmentError("Judge selected a candidate that was not offered")
        if judgment.question is not None:
            raise CandidateJudgmentError("SELECT judgment cannot include a question")
        return
    if judgment.selected_candidate_id is not None:
        raise CandidateJudgmentError("Non-SELECT judgment cannot include a candidate ID")
    if judgment.action is CandidateJudgeAction.ASK_USER:
        if not (judgment.question or "").strip():
            raise CandidateJudgmentError("ASK_USER judgment requires a question")
        return
    if judgment.question is not None:
        raise CandidateJudgmentError("NO_MATCH judgment cannot include a question")
