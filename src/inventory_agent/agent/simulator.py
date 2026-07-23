"""Opt-in live evaluation CLI for the no-write inventory-agent spike."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from openai import AsyncOpenAI

from inventory_agent.agent.models import (
    AttributeValue,
    CatalogVariant,
    SimulationProposal,
    TrackingMode,
    TransactionRecord,
)
from inventory_agent.agent.runtime import (
    AgentReply,
    InventoryAgentSession,
    OpenAIResponsesAgentModel,
)
from inventory_agent.agent.tools import SimulatedInventoryTools
from inventory_agent.config import Settings
from inventory_agent.processing.worker import _required_secret


@dataclass(frozen=True, slots=True)
class EvaluationScenario:
    name: str
    messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScenarioVerdict:
    passed: bool
    reason: str


SCENARIOS = (
    EvaluationScenario(
        name="unrelated_chat",
        messages=("Tell me a joke about databases.",),
    ),
    EvaluationScenario(
        name="exact_receipt",
        messages=("A delivery came in: 3 of part number ABC-123.",),
    ),
    EvaluationScenario(
        name="different_product_generation",
        messages=(
            "ok hmm one more thing, i want got another batch of nintendo switch contrler, "
            "but this one is the first edition. i got 6 in this batch",
        ),
    ),
    EvaluationScenario(
        name="multi_turn_variant_split",
        messages=(
            "I received 4 classic t-shirts.",
            "They are all black, and 2 are L and 2 are XS.",
        ),
    ),
    EvaluationScenario(
        name="transaction_reversal",
        messages=("Undo the last receipt because the quantity was entered incorrectly.",),
    ),
)


def demo_catalog() -> list[CatalogVariant]:
    return [
        CatalogVariant(
            variant_id="variant-widget-abc",
            item_name="Industrial Widget",
            variant_name="Standard",
            sku="ABC-123",
            on_hand=Decimal("10"),
        ),
        CatalogVariant(
            variant_id="variant-switch-2-controller",
            item_name="Nintendo Switch 2 Controller",
            sku="NS2-CONTROLLER",
            on_hand=Decimal("5"),
        ),
        CatalogVariant(
            variant_id="variant-shirt-black-l",
            item_name="Classic T-Shirt",
            variant_name="Black / L",
            sku="TSHIRT-BLK-L",
            attributes=[
                AttributeValue(key="colour", value="black"),
                AttributeValue(key="size", value="L"),
            ],
            on_hand=Decimal("8"),
        ),
        CatalogVariant(
            variant_id="variant-shirt-black-xs",
            item_name="Classic T-Shirt",
            variant_name="Black / XS",
            sku="TSHIRT-BLK-XS",
            attributes=[
                AttributeValue(key="colour", value="black"),
                AttributeValue(key="size", value="XS"),
            ],
            on_hand=Decimal("4"),
        ),
        CatalogVariant(
            variant_id="variant-paracetamol-500",
            item_name="Paracetamol 500 mg",
            sku="MED-PARA-500",
            tracking_mode=TrackingMode.LOT,
            on_hand=Decimal("120"),
        ),
    ]


def demo_transactions() -> list[TransactionRecord]:
    return [
        TransactionRecord(
            transaction_id="txn-receipt-100",
            transaction_type="receipt",
            occurred_at="2026-07-23T09:00:00+08:00",
            summary="Received 3 Industrial Widget ABC-123",
        ),
        TransactionRecord(
            transaction_id="txn-issue-099",
            transaction_type="issue",
            occurred_at="2026-07-22T17:00:00+08:00",
            summary="Issued 1 Nintendo Switch 2 Controller",
        ),
    ]


async def run_live_scenarios(*, scenario_name: str | None) -> int:
    settings = Settings()
    api_key = _required_secret(settings.openai_api_key, "OPENAI_API_KEY")
    selected = [
        scenario
        for scenario in SCENARIOS
        if scenario_name is None or scenario.name == scenario_name
    ]
    if not selected:
        names = ", ".join(scenario.name for scenario in SCENARIOS)
        raise ValueError(f"unknown scenario {scenario_name!r}; choose one of: {names}")

    client = AsyncOpenAI(api_key=api_key)
    total_tokens = 0
    failed = 0
    try:
        for scenario in selected:
            tools = SimulatedInventoryTools(
                catalog=demo_catalog(),
                transactions=demo_transactions(),
            )
            session = InventoryAgentSession(
                model=OpenAIResponsesAgentModel(
                    client=client,
                    model=settings.inventory_agent_model,
                    reasoning_effort=settings.inventory_agent_reasoning_effort,
                ),
                tools=tools,
            )
            print(f"\nSCENARIO {scenario.name}")
            replies: list[AgentReply] = []
            for message in scenario.messages:
                print(f"USER: {message}")
                reply = await session.handle(message)
                replies.append(reply)
                _print_reply(reply)
                total_tokens += reply.total_tokens
            print(
                "PROPOSALS: "
                + json.dumps(
                    [proposal.model_dump(mode="json") for proposal in tools.proposals],
                    indent=2,
                )
            )
            verdict = evaluate_scenario(
                scenario_name=scenario.name,
                replies=replies,
                proposals=tools.proposals,
            )
            print(f"VERDICT: {'PASS' if verdict.passed else 'FAIL'} — {verdict.reason}")
            if not verdict.passed:
                failed += 1
    finally:
        await client.close()
    print(f"\nTOTAL_TOKENS: {total_tokens}")
    print(f"SUMMARY: {len(selected) - failed}/{len(selected)} scenarios passed")
    return 1 if failed else 0


def evaluate_scenario(
    *,
    scenario_name: str,
    replies: list[AgentReply],
    proposals: list[SimulationProposal],
) -> ScenarioVerdict:
    """Apply small deterministic checks to the model's observable behavior."""

    traces = [trace for reply in replies for trace in reply.tool_traces]
    final_text = replies[-1].text.casefold() if replies else ""
    if scenario_name == "unrelated_chat":
        passed = not traces and not proposals and "inventory" in final_text
        return ScenarioVerdict(
            passed=passed,
            reason=(
                "declined unrelated chat without tools"
                if passed
                else "unrelated chat used tools, created a proposal, or lacked a scope response"
            ),
        )
    if scenario_name == "exact_receipt":
        passed = _has_stock_proposal(
            proposals,
            operation="ADD",
            expected_lines={"variant-widget-abc": "3"},
        )
        return ScenarioVerdict(
            passed=passed,
            reason=(
                "grounded the exact SKU and proposed quantity 3"
                if passed
                else "did not create the expected grounded receipt proposal"
            ),
        )
    if scenario_name == "different_product_generation":
        passed = not proposals and (
            "new" in final_text or "catalog" in final_text or "first" in final_text
        )
        return ScenarioVerdict(
            passed=passed,
            reason=(
                "did not collapse first-generation Switch into Switch 2"
                if passed
                else "incorrectly proposed a match or failed to clarify the distinct product"
            ),
        )
    if scenario_name == "multi_turn_variant_split":
        passed = _has_stock_proposal(
            proposals,
            operation="ADD",
            expected_lines={
                "variant-shirt-black-l": "2",
                "variant-shirt-black-xs": "2",
            },
        )
        return ScenarioVerdict(
            passed=passed,
            reason=(
                "used the follow-up and split L/XS into two grounded lines"
                if passed
                else "did not create the expected two-line variant proposal"
            ),
        )
    if scenario_name == "transaction_reversal":
        passed = any(
            proposal.operation == "REVERSE"
            and proposal.payload.get("transaction_id") == "txn-receipt-100"
            for proposal in proposals
        )
        return ScenarioVerdict(
            passed=passed,
            reason=(
                "read the ledger and proposed a compensating reversal"
                if passed
                else "did not propose reversal of the latest receipt"
            ),
        )
    return ScenarioVerdict(passed=False, reason="scenario has no evaluator")


def _has_stock_proposal(
    proposals: list[SimulationProposal],
    *,
    operation: str,
    expected_lines: dict[str, str],
) -> bool:
    for proposal in proposals:
        if proposal.operation != operation:
            continue
        lines = proposal.payload.get("lines")
        if not isinstance(lines, list):
            continue
        actual: dict[str, str] = {}
        for line in lines:
            if not isinstance(line, dict):
                continue
            variant_id = line.get("variant_id")
            quantity = line.get("quantity")
            if isinstance(variant_id, str):
                actual[variant_id] = str(quantity)
        if actual == expected_lines:
            return True
    return False


def _print_reply(reply: AgentReply) -> None:
    for trace in reply.tool_traces:
        print(f"TOOL {trace.name}: {json.dumps(trace.arguments, default=str)}")
        print(f"RESULT: {trace.output}")
    print(f"ASSISTANT: {reply.text}")
    print(
        f"USAGE: model={reply.model} input={reply.input_tokens} "
        f"output={reply.output_tokens} total={reply.total_tokens}"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run live, no-write inventory-agent evaluation scenarios"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required opt-in because this command spends OpenAI API credits",
    )
    parser.add_argument(
        "--scenario",
        choices=[scenario.name for scenario in SCENARIOS],
        help="Run one scenario instead of the full evaluation set",
    )
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required; the simulator makes billable OpenAI API calls")
    raise SystemExit(asyncio.run(run_live_scenarios(scenario_name=args.scenario)))


if __name__ == "__main__":
    main()
