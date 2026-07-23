"""Tests for Telegram proposal confirmation rendering."""

from decimal import Decimal
from uuid import UUID

from inventory_agent.catalog.models import CatalogItemCreationView, CatalogTrackingMode
from inventory_agent.telegram.callbacks import CallbackAction, decode_callback
from inventory_agent.telegram.confirmation import (
    CandidateChoice,
    ProposalLineView,
    render_applied_transaction,
    render_catalog_item_confirmation,
    render_catalog_item_details_prompt,
    render_proposal_confirmation,
    render_reversal_confirmation,
    render_reversal_reason_prompt,
)

PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")
LINE_ID = UUID("41000000-0000-0000-0000-000000000001")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000001")
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000001")
REVERSAL_REQUEST_ID = UUID("70000000-0000-0000-0000-000000000001")
CATALOG_REQUEST_ID = UUID("71000000-0000-0000-0000-000000000001")


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
    assert "Resolve every unmatched line" in message.text


def test_not_found_line_offers_add_new_or_choose_existing_before_candidates() -> None:
    message = render_proposal_confirmation(
        proposal_id=PROPOSAL_ID,
        intent_label="stock receipt",
        lines=[
            ProposalLineView(
                proposal_line_id=LINE_ID,
                description="Purple Widget",
                quantity=Decimal("4"),
                unit="units",
                match_decision="not_found",
                candidate_choices=[
                    CandidateChoice(item_variant_id=VARIANT_ID, label="Anchor Butter")
                ],
            )
        ],
    )

    actions = [
        decode_callback(button.callback_data).action for button in message.inline_keyboard[0]
    ]
    assert actions == [
        CallbackAction.ADD_NEW_ITEM,
        CallbackAction.SHOW_EXISTING_ITEMS,
    ]
    assert [button.text for button in message.inline_keyboard[0]] == [
        "Add new item",
        "Choose existing",
    ]
    assert "No confident catalog match" in message.text


def test_multi_line_buttons_include_their_line_numbers() -> None:
    second_line_id = UUID("41000000-0000-0000-0000-000000000002")
    message = render_proposal_confirmation(
        proposal_id=PROPOSAL_ID,
        intent_label="stock receipt",
        lines=[
            ProposalLineView(
                proposal_line_id=LINE_ID,
                description="Purple Widget",
                quantity=Decimal("4"),
                unit=None,
                match_decision="not_found",
            ),
            ProposalLineView(
                proposal_line_id=second_line_id,
                description="Orange Widget",
                quantity=Decimal("2"),
                unit=None,
                match_decision="not_found",
            ),
        ],
    )

    assert [button.text for button in message.inline_keyboard[0]] == [
        "1: Add new item",
        "1: Choose existing",
    ]
    assert [button.text for button in message.inline_keyboard[1]] == [
        "2: Add new item",
        "2: Choose existing",
    ]


def test_catalog_detail_and_confirmation_messages_use_expected_actions() -> None:
    details_view = CatalogItemCreationView(
        request_id=CATALOG_REQUEST_ID,
        status="awaiting_details",
        suggested_name="Purple Widget",
        suggested_sku="ZX-999",
        suggested_base_unit="each",
        suggested_tracking_mode="simple",
    )
    prompt = render_catalog_item_details_prompt(details_view)
    cancel = decode_callback(prompt.inline_keyboard[0][0].callback_data)
    assert cancel.action is CallbackAction.CANCEL_NEW_ITEM
    assert "item name as “Purple Widget”" in prompt.text
    assert "Reply naturally" in prompt.text
    assert "prototype currently supports simple tracking" in prompt.text
    assert "Name:" not in prompt.text

    confirmation = render_catalog_item_confirmation(
        details_view.model_copy(
            update={
                "status": "awaiting_confirmation",
                "name": "Purple Widget",
                "sku": "ZX-999",
                "base_unit": "each",
                "tracking_mode": CatalogTrackingMode.SIMPLE,
                "attributes": {"colour": "purple"},
            }
        )
    )
    actions = [
        decode_callback(button.callback_data).action for button in confirmation.inline_keyboard[0]
    ]
    assert actions == [CallbackAction.CONFIRM_NEW_ITEM, CallbackAction.CANCEL_NEW_ITEM]


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
