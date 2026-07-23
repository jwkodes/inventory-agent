"""Tests for constrained OpenAI candidate judgment."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from inventory_agent.extraction.schema import ExtractedCommandLine
from inventory_agent.matching.judge import (
    CandidateJudgeOutput,
    CandidateJudgmentError,
    OpenAICandidateJudge,
)
from inventory_agent.matching.models import InventoryCandidate

VARIANT_ID = UUID("21000000-0000-0000-0000-000000000001")


class FakeResponses:
    def __init__(self, output: CandidateJudgeOutput) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.output)


class FakeOpenAI:
    def __init__(self, output: CandidateJudgeOutput) -> None:
        self.responses = FakeResponses(output)


def line() -> ExtractedCommandLine:
    return ExtractedCommandLine(
        source_text="six first edition controllers",
        item_reference={"type": "NAME", "value": "nintendo switch controller"},
        description="nintendo switch controller",
        quantity="6",
        unit=None,
        attributes=[{"key": "edition", "value": "first edition"}],
    )


def candidate() -> InventoryCandidate:
    return InventoryCandidate(
        item_variant_id=VARIANT_ID,
        item_id=UUID("20000000-0000-0000-0000-000000000001"),
        item_name="Nintendo Switch Controller",
        variant_name="Switch 2",
        sku="SW2-CONTROLLER",
        base_unit="each",
        tracking_mode="simple",
        match_method="semantic_rerank",
        match_score=Decimal("0.91"),
        match_evidence={
            "variant_attributes": {"generation": "second"},
            "attribute_matching_roles": {"generation": "discriminator"},
        },
    )


async def test_judge_passes_candidate_attributes_and_company_roles() -> None:
    client = FakeOpenAI(
        CandidateJudgeOutput(
            action="NO_MATCH",
            selected_candidate_id=None,
            question=None,
            reason="First edition conflicts with the Switch 2 candidate.",
        )
    )
    judge = OpenAICandidateJudge(client=client, model="gpt-test")  # type: ignore[arg-type]

    result = await judge.judge(line=line(), candidates=[candidate()])

    assert result.action.value == "NO_MATCH"
    payload = str(client.responses.calls[0]["input"])
    assert '"variant_attributes": {"generation": "second"}' in payload
    assert '"generation": "discriminator"' in payload
    assert client.responses.calls[0]["store"] is False


async def test_judge_rejects_an_unoffered_selected_id() -> None:
    client = FakeOpenAI(
        CandidateJudgeOutput(
            action="SELECT",
            selected_candidate_id=UUID("21000000-0000-0000-0000-000000000099"),
            question=None,
            reason="Selected.",
        )
    )
    judge = OpenAICandidateJudge(client=client, model="gpt-test")  # type: ignore[arg-type]

    with pytest.raises(CandidateJudgmentError, match="not offered"):
        await judge.judge(line=line(), candidates=[candidate()])
