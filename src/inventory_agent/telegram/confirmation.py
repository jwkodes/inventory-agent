"""Render proposal review messages and inline keyboards."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from inventory_agent.catalog.models import CatalogItemCreationView
from inventory_agent.telegram.callbacks import CallbackAction, CallbackCommand, encode_callback


class CandidateChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_variant_id: UUID
    label: str


class ProposalLineView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_line_id: UUID
    description: str
    quantity: Decimal
    unit: str | None
    matched_label: str | None = None
    candidate_choices: list[CandidateChoice] = Field(default_factory=list)
    match_decision: str | None = None
    clarification_question: str | None = None
    show_candidates: bool = False


class InlineButton(BaseModel):
    text: str
    callback_data: str


class ConfirmationMessage(BaseModel):
    text: str
    inline_keyboard: list[list[InlineButton]]


class ProposalConfirmationView(BaseModel):
    """Database projection required to render one proposal review."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    intent: str
    lines: list[ProposalLineView]


def render_proposal_confirmation(
    *,
    proposal_id: UUID,
    intent_label: str,
    lines: list[ProposalLineView],
) -> ConfirmationMessage:
    text_lines = [f"Review {intent_label}:"]
    keyboard: list[list[InlineButton]] = []
    unresolved = False
    clarification_prompt_shown = False
    has_multiple_lines = len(lines) > 1

    for index, line in enumerate(lines, start=1):
        button_prefix = f"{index}: " if has_multiple_lines else ""
        unit = f" {line.unit}" if line.unit else ""
        match = line.matched_label or "match required"
        text_lines.append(f"{index}. {line.quantity}{unit} — {line.description} → {match}")
        if line.matched_label is None:
            unresolved = True
            if line.match_decision == "clarification_required" and line.clarification_question:
                if not clarification_prompt_shown:
                    text_lines.append(
                        f"I need one detail: {line.clarification_question.strip()} "
                        "Reply naturally in a new message."
                    )
                    clarification_prompt_shown = True
            elif line.match_decision == "not_found" and not line.show_candidates:
                subject = f"line {index}" if has_multiple_lines else "this item"
                text_lines.append(
                    f"No confident catalog match was found for {subject}. "
                    "Add it as a new item or choose an existing item."
                )
                keyboard.append(
                    [
                        InlineButton(
                            text=f"{button_prefix}Add new item",
                            callback_data=encode_callback(
                                CallbackCommand(
                                    CallbackAction.ADD_NEW_ITEM,
                                    line.proposal_line_id,
                                )
                            ),
                        ),
                        InlineButton(
                            text=f"{button_prefix}Choose existing",
                            callback_data=encode_callback(
                                CallbackCommand(
                                    CallbackAction.SHOW_EXISTING_ITEMS,
                                    line.proposal_line_id,
                                )
                            ),
                        ),
                    ]
                )
            else:
                for choice in line.candidate_choices:
                    keyboard.append(
                        [
                            InlineButton(
                                text=f"{button_prefix}{choice.label}"[:64],
                                callback_data=encode_callback(
                                    CallbackCommand(
                                        action=CallbackAction.SELECT_VARIANT,
                                        target_id=line.proposal_line_id,
                                        choice_id=choice.item_variant_id,
                                    )
                                ),
                            )
                        ]
                    )

    if not unresolved:
        keyboard.append(
            [
                InlineButton(
                    text="Confirm",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CONFIRM_PROPOSAL, proposal_id)
                    ),
                ),
                InlineButton(
                    text="Cancel",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CANCEL_PROPOSAL, proposal_id)
                    ),
                ),
            ]
        )
    else:
        text_lines.append("Resolve every unmatched line before confirming.")
        keyboard.append(
            [
                InlineButton(
                    text="Cancel",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CANCEL_PROPOSAL, proposal_id)
                    ),
                )
            ]
        )

    return ConfirmationMessage(text="\n".join(text_lines), inline_keyboard=keyboard)


def render_catalog_item_details_prompt(view: CatalogItemCreationView) -> ConfirmationMessage:
    """Ask for catalog facts without imposing a user-facing serialization format."""

    understood = (
        f"I understood the item name as “{view.suggested_name}”. " if view.suggested_name else ""
    )
    return ConfirmationMessage(
        text=(
            "No existing item was matched. "
            f"{understood}"
            "To create it, tell me:\n"
            "• the item name\n"
            "• its SKU, part number, or internal product code\n"
            "• how it is counted, such as each, box, bottle, kg, or litre\n"
            "• any useful attributes, such as colour or size (optional)\n\n"
            "Reply naturally or use any list format you prefer. "
            "This prototype currently supports simple tracking."
        ),
        inline_keyboard=[
            [
                InlineButton(
                    text="Cancel item creation",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CANCEL_NEW_ITEM, view.request_id)
                    ),
                )
            ]
        ],
    )


def render_catalog_item_confirmation(view: CatalogItemCreationView) -> ConfirmationMessage:
    """Review a complete item draft before catalog creation."""

    if not all((view.name, view.sku, view.base_unit, view.tracking_mode)):
        raise ValueError("Catalog item confirmation view is incomplete")
    tracking_mode = view.tracking_mode
    if tracking_mode is None:
        raise ValueError("Catalog item tracking mode is missing")
    return ConfirmationMessage(
        text=(
            "Create this catalog item?\n"
            f"Name: {view.name}\n"
            f"SKU: {view.sku}\n"
            f"Base unit: {view.base_unit}\n"
            f"Tracking: {tracking_mode.value}\n"
            f"Attributes: {view.attributes}"
        ),
        inline_keyboard=[
            [
                InlineButton(
                    text="Create item",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CONFIRM_NEW_ITEM, view.request_id)
                    ),
                ),
                InlineButton(
                    text="Cancel",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CANCEL_NEW_ITEM, view.request_id)
                    ),
                ),
            ]
        ],
    )


def render_applied_transaction(transaction_id: UUID) -> ConfirmationMessage:
    """Offer a complete reversal after a proposal has been applied."""

    return ConfirmationMessage(
        text="Inventory updated.",
        inline_keyboard=[
            [
                InlineButton(
                    text="Reverse transaction",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.REVERSE_TRANSACTION, transaction_id)
                    ),
                )
            ]
        ],
    )


def render_reversal_reason_prompt(request_id: UUID) -> ConfirmationMessage:
    """Ask for free text while retaining an explicit cancellation path."""

    return ConfirmationMessage(
        text=(
            "Reply with the reason for reversing this transaction. "
            "The inventory will not change until you confirm."
        ),
        inline_keyboard=[
            [
                InlineButton(
                    text="Cancel reversal",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CANCEL_REVERSAL, request_id)
                    ),
                )
            ]
        ],
    )


def render_reversal_confirmation(*, request_id: UUID, reason: str) -> ConfirmationMessage:
    """Render the final human checkpoint before applying a reversal."""

    return ConfirmationMessage(
        text=(
            "Review complete transaction reversal:\n"
            f"Reason: {reason}\n"
            "This will create an opposite inventory transaction."
        ),
        inline_keyboard=[
            [
                InlineButton(
                    text="Confirm reversal",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CONFIRM_REVERSAL, request_id)
                    ),
                ),
                InlineButton(
                    text="Cancel",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CANCEL_REVERSAL, request_id)
                    ),
                ),
            ]
        ],
    )
