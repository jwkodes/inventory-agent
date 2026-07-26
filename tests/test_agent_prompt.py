"""Contract tests for the inventory agent's catalog clarification policy."""

from inventory_agent.agent.prompt import INSTRUCTIONS, PROMPT_VERSION


def test_new_item_attributes_are_optional_but_preserved() -> None:
    assert PROMPT_VERSION == "inventory-agent-spike-v13"
    assert "Custom attributes" in INSTRUCTIONS
    assert "label it optional" in INSTRUCTIONS
    assert "allow the user to skip it" in INSTRUCTIONS
    assert "Every attribute question must briefly explain its evidence" in INSTRUCTIONS
    assert "new product that resembles a catalog item" in INSTRUCTIONS
    assert "already known unambiguously" in INSTRUCTIONS
    assert "Preserve every attribute the user supplies" in INSTRUCTIONS
    assert "similar catalog item" in INSTRUCTIONS


def test_new_items_currently_require_sku_and_cannot_be_falsely_accepted() -> None:
    normalized = " ".join(INSTRUCTIONS.split())
    assert "SKU or internal code is mandatory" in normalized
    assert "If the user asks to omit it, do not agree" in normalized
    assert "do not claim that a proposal is ready" in normalized
    assert "ask what SKU or internal code to use" in normalized


def test_generic_count_units_do_not_trigger_catalog_clarification() -> None:
    normalized = " ".join(INSTRUCTIONS.split())
    assert "silently use canonical base unit `each`" in normalized
    assert "never ask the user to choose between them" in normalized
    assert '"buy 1 Nintendo Switch second edition" is `1 each`' in normalized
    assert "box versus individual tablets" in normalized


def test_confirmation_claims_require_authoritative_deterministic_events() -> None:
    normalized = " ".join(INSTRUCTIONS.split())
    assert "exact standalone `Confirm`" in normalized
    assert "exact standalone `Cancel`" in normalized
    assert "Never interpret conversational text as proof" in normalized


def test_telegram_tables_use_fenced_fixed_width_text() -> None:
    assert "does not render GitHub-style Markdown pipe tables" in INSTRUCTIONS
    assert "fixed-width plain-text table" in INSTRUCTIONS
    assert "fenced ```text code block" in INSTRUCTIONS


def test_transaction_corrections_require_broad_reads_and_complete_reversal() -> None:
    normalized = " ".join(INSTRUCTIONS.split())
    assert "targeted_count=0" in INSTRUCTIONS
    assert "unfiltered" in INSTRUCTIONS
    assert "recent-transaction" in INSTRUCTIONS
    assert "complete transactions, not individual lines" in INSTRUCTIONS
    assert "corrected replacement transaction" in INSTRUCTIONS
    assert "additional deduction on top" in INSTRUCTIONS
    assert "automatically present its separate" in normalized
    assert "must not need to send another message" in normalized
    assert "full transaction UUID" in normalized
    assert "`transaction_type`, `status`, and `reversed`" in normalized
    assert 'as "active"' in normalized


def test_category_totals_are_exhaustive_and_balance_disputes_trigger_rereads() -> None:
    normalized = " ".join(INSTRUCTIONS.split())
    assert '"how many hairdryers do I have?"' in normalized
    assert "not only the first or most recently discussed variant" in normalized
    assert "per-variant breakdown and total" in normalized
    assert "Ranked candidate results can contain incidental items" in normalized
    assert "asks you to recheck a reported balance" in normalized
    assert "read inventory again during that message" in normalized
    assert "before rechecking" in normalized
