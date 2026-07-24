"""Contract tests for the inventory agent's catalog clarification policy."""

from inventory_agent.agent.prompt import INSTRUCTIONS, PROMPT_VERSION


def test_new_item_attributes_are_optional_but_preserved() -> None:
    assert PROMPT_VERSION == "inventory-agent-spike-v6"
    assert "Custom attributes" in INSTRUCTIONS
    assert "label it optional" in INSTRUCTIONS
    assert "allow the user to skip it" in INSTRUCTIONS
    assert "Every attribute question must briefly explain its evidence" in INSTRUCTIONS
    assert "new product that resembles a catalog item" in INSTRUCTIONS
    assert "already known unambiguously" in INSTRUCTIONS
    assert "Preserve every attribute the user supplies" in INSTRUCTIONS
    assert "similar catalog item" in INSTRUCTIONS


def test_telegram_tables_use_fenced_fixed_width_text() -> None:
    assert "does not render GitHub-style Markdown pipe tables" in INSTRUCTIONS
    assert "fixed-width plain-text table" in INSTRUCTIONS
    assert "fenced ```text code block" in INSTRUCTIONS


def test_transaction_corrections_require_broad_reads_and_complete_reversal() -> None:
    assert "targeted_count=0" in INSTRUCTIONS
    assert "unfiltered" in INSTRUCTIONS
    assert "recent-transaction" in INSTRUCTIONS
    assert "complete transactions, not individual lines" in INSTRUCTIONS
    assert "corrected replacement transaction" in INSTRUCTIONS
    assert "additional deduction on top" in INSTRUCTIONS
