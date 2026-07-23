"""Deterministic acceptance checks for live scenario results."""

from inventory_agent.agent.models import SimulationProposal
from inventory_agent.agent.runtime import AgentReply
from inventory_agent.agent.simulator import evaluate_scenario


def reply(text: str) -> AgentReply:
    return AgentReply(
        text=text,
        response_id="response",
        model="model",
        prompt_version="prompt",
    )


def test_variant_split_scenario_passes_only_for_two_grounded_lines() -> None:
    proposal = SimulationProposal(
        proposal_id="proposal",
        operation="ADD",
        payload={
            "lines": [
                {"variant_id": "variant-shirt-black-l", "quantity": "2"},
                {"variant_id": "variant-shirt-black-xs", "quantity": "2"},
            ]
        },
    )

    verdict = evaluate_scenario(
        scenario_name="multi_turn_variant_split",
        replies=[reply("Confirmation is required.")],
        proposals=[proposal],
    )

    assert verdict.passed


def test_first_generation_scenario_rejects_switch_two_proposal() -> None:
    proposal = SimulationProposal(
        proposal_id="proposal",
        operation="ADD",
        payload={
            "lines": [
                {"variant_id": "variant-switch-2-controller", "quantity": "6"},
            ]
        },
    )

    verdict = evaluate_scenario(
        scenario_name="different_product_generation",
        replies=[reply("Please confirm.")],
        proposals=[proposal],
    )

    assert not verdict.passed
