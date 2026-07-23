"""Tests for Telegram proposal confirmation rendering."""

from decimal import Decimal
from uuid import UUID

from inventory_agent.telegram.callbacks import CallbackAction, decode_callback
from inventory_agent.telegram.confirmation import (
    CandidateChoice,
    ProposalLineView,
    render_applied_transaction,
    render_proposal_confirmation,
    render_reversal_confirmation,
    render_reversal_reason_prompt,
)

PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")
LINE_ID = UUID("41000000-0000-0000-0000-000000000001")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000001")
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000001")
REVERSAL_REQUEST_ID = UUID("70000000-0000-0000-0000-000000000001")


def test_resolved_proposal_has_confirm_and_cancel_buttons() -> None:
    message = render_proposal_confirmation(
        proposal_id=PROPOSAL_ID,
        intent_label="stock receipt",
        lines=[
            ProposalLineView(
                proposal_line_id=LINE_ID,
                description="Anchor Butter",
                quantity=Decimal("3"),
                unit="each",
                matched_label="Anchor Butter 500g",
            )
        ],
    )

    actions = [
        decode_callback(button.callback_data).action for button in message.inline_keyboard[-1]
    ]
    assert actions == [CallbackAction.CONFIRM_PROPOSAL, CallbackAction.CANCEL_PROPOSAL]
    assert "3 each" in message.text


def test_unresolved_proposal_requires_candidate_selection() -> None:
    message = render_proposal_confirmation(
        proposal_id=PROPOSAL_ID,
        intent_label="stock receipt",
        lines=[
            ProposalLineView(
                proposal_line_id=LINE_ID,
                description="butter",
                quantity=Decimal("3"),
                unit=None,
                candidate_choices=[
                    CandidateChoice(item_variant_id=VARIANT_ID, label="Anchor Butter 500g")
                ],
            )
        ],
    )

    selection = decode_callback(message.inline_keyboard[0][0].callback_data)
    final_actions = [
        decode_callback(button.callback_data).action for button in message.inline_keyboard[-1]
    ]
    assert selection.action is CallbackAction.SELECT_VARIANT
    assert selection.target_id == LINE_ID
    assert selection.choice_id == VARIANT_ID
    assert final_actions == [CallbackAction.CANCEL_PROPOSAL]
    assert "Choose a match" in message.text


def test_applied_transaction_and_reversal_prompts_use_expected_actions() -> None:
    applied = render_applied_transaction(TRANSACTION_ID)
    reverse = decode_callback(applied.inline_keyboard[0][0].callback_data)
    assert reverse.action is CallbackAction.REVERSE_TRANSACTION
    assert reverse.target_id == TRANSACTION_ID

    reason_prompt = render_reversal_reason_prompt(REVERSAL_REQUEST_ID)
    cancel_prompt = decode_callback(reason_prompt.inline_keyboard[0][0].callback_data)
    assert cancel_prompt.action is CallbackAction.CANCEL_REVERSAL

    confirmation = render_reversal_confirmation(
        request_id=REVERSAL_REQUEST_ID,
        reason="Duplicate delivery",
    )
    actions = [
        decode_callback(button.callback_data).action for button in confirmation.inline_keyboard[0]
    ]
    assert actions == [CallbackAction.CONFIRM_REVERSAL, CallbackAction.CANCEL_REVERSAL]
    assert "Duplicate delivery" in confirmation.text
