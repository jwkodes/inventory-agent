"""Contract tests for the inventory agent's catalog clarification policy."""

from inventory_agent.agent.prompt import INSTRUCTIONS, PROMPT_VERSION


def test_new_item_attributes_are_optional_but_preserved() -> None:
    assert PROMPT_VERSION == "inventory-agent-spike-v4"
    assert "Custom attributes" in INSTRUCTIONS
    assert "label it optional" in INSTRUCTIONS
    assert "allow the user to skip it" in INSTRUCTIONS
    assert "Every attribute question must briefly explain its evidence" in INSTRUCTIONS
    assert "new product that resembles a catalog item" in INSTRUCTIONS
    assert "already known unambiguously" in INSTRUCTIONS
    assert "Preserve every attribute the user supplies" in INSTRUCTIONS
    assert "similar catalog item" in INSTRUCTIONS
