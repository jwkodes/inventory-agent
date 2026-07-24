"""Render proposal review messages and inline keyboards."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

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
    operation_label, line_action = _movement_labels(intent_label)
    text_lines = [f"Review {operation_label}:"]
    keyboard: list[list[InlineButton]] = []
    unresolved = False
    clarification_prompt_shown = False
    has_multiple_lines = len(lines) > 1
    has_multiple_unresolved_lines = sum(line.matched_label is None for line in lines) > 1

    for index, line in enumerate(lines, start=1):
        unit = f" {line.unit}" if line.unit else ""
        quantity = format(line.quantity.normalize(), "f")
        match = line.matched_label or "match required"
        text_lines.append(f"{index}. {line_action} {quantity}{unit} — {line.description} → {match}")
        if line.matched_label is None:
            unresolved = True
            if line.match_decision == "clarification_required" and line.clarification_question:
                if not clarification_prompt_shown:
                    text_lines.append(
                        f"❓ **One detail needed:** {line.clarification_question.strip()} "
                        "Reply naturally in a new message."
                    )
                    clarification_prompt_shown = True
            elif line.match_decision == "not_found" and not line.show_candidates:
                subject = f"line {index}" if has_multiple_lines else "this item"
                text_lines.append(
                    f"🔎 **No confident match:** No catalog match was found for {subject}. "
                    "Add it as a new item or choose an existing item."
                )
                keyboard.append(
                    [
                        InlineButton(
                            text=_line_action_label(
                                "Add new item",
                                line.description,
                                disambiguate=has_multiple_unresolved_lines,
                            ),
                            callback_data=encode_callback(
                                CallbackCommand(
                                    CallbackAction.ADD_NEW_ITEM,
                                    line.proposal_line_id,
                                )
                            ),
                        ),
                        InlineButton(
                            text=_line_action_label(
                                "Choose existing",
                                line.description,
                                disambiguate=has_multiple_unresolved_lines,
                            ),
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
                                text=_candidate_label(
                                    choice.label,
                                    line.description,
                                    disambiguate=has_multiple_unresolved_lines,
                                ),
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
        text_lines.insert(0, f"⏳ **Pending {operation_label}**")
        text_lines.append("No inventory has changed yet.")
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
        text_lines.insert(0, "⚠️ **Action needed**")
        text_lines.append(
            "Resolve every unmatched line before confirming. No inventory has changed."
        )
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


def _movement_labels(intent_label: str) -> tuple[str, str]:
    if intent_label == "stock receipt":
        return "stock addition", "➕ ADD"
    if intent_label == "stock issue":
        return "stock deduction", "➖ DEDUCT"
    if intent_label == "stock adjustment":
        return "stock adjustment", "↕️ ADJUST"
    return intent_label, "↕️ CHANGE"


def _line_action_label(
    action: str,
    description: str,
    *,
    disambiguate: bool,
) -> str:
    if not disambiguate:
        return action
    return f"{action}: {description}"[:64]


def _candidate_label(
    candidate: str,
    description: str,
    *,
    disambiguate: bool,
) -> str:
    if not disambiguate:
        return candidate[:64]
    return f"{description} → {candidate}"[:64]


def render_catalog_item_details_prompt(view: CatalogItemCreationView) -> ConfirmationMessage:
    """Ask for catalog facts without imposing a user-facing serialization format."""

    understood = (
        f"I understood the item name as “{view.suggested_name}”. " if view.suggested_name else ""
    )
    return ConfirmationMessage(
        text=(
            "📝 **New item details needed**\n"
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
            "⏳ **Pending catalog change**\n"
            "Create this catalog item?\n"
            f"Name: {view.name}\n"
            f"SKU: {view.sku}\n"
            f"Base unit: {view.base_unit}\n"
            f"Tracking: {tracking_mode.value}\n"
            f"Attributes: {view.attributes}\n"
            "The item has not been created yet."
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


def render_applied_transaction(
    transaction_id: UUID,
    *,
    transaction_type: str,
    applied_at: datetime,
    display_timezone: ZoneInfo,
) -> ConfirmationMessage:
    """Offer a complete reversal after a proposal has been applied."""

    heading, result = _applied_transaction_copy(transaction_type)
    return ConfirmationMessage(
        text=(
            f"{heading}\n"
            f"{result}\n"
            f"🧾 Transaction ID: `{transaction_id}`\n"
            f"🕒 Transaction time: {_format_timestamp(applied_at, display_timezone)}"
        ),
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


def _applied_transaction_copy(transaction_type: str) -> tuple[str, str]:
    if transaction_type == "receive":
        return "✅ **Stock added**", "The addition transaction was applied successfully."
    if transaction_type == "issue":
        return "✅ **Stock deducted**", "The deduction transaction was applied successfully."
    if transaction_type == "adjustment":
        return "✅ **Stock adjusted**", "The adjustment transaction was applied successfully."
    raise ValueError("Applied proposal has an unsupported transaction type")


def render_reversal_reason_prompt(request_id: UUID) -> ConfirmationMessage:
    """Ask for free text while retaining an explicit cancellation path."""

    return ConfirmationMessage(
        text=(
            "❓ **Reversal reason needed**\n"
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


def render_reversal_confirmation(
    *,
    request_id: UUID,
    original_transaction_id: UUID,
    reason: str,
    original_transaction_applied_at: datetime,
    display_timezone: ZoneInfo,
) -> ConfirmationMessage:
    """Render the final human checkpoint before applying a reversal."""

    return ConfirmationMessage(
        text=(
            "⏳ **Pending reversal confirmation**\n"
            "Review complete transaction reversal:\n"
            f"🧾 Original transaction ID: `{original_transaction_id}`\n"
            "🕒 Original transaction time: "
            f"{_format_timestamp(original_transaction_applied_at, display_timezone)}\n"
            f"Reason: {reason}\n"
            "Confirming will create an opposite inventory transaction. "
            "No inventory has changed yet."
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


def render_reversal_applied(
    *,
    transaction_id: UUID,
    applied_at: datetime,
    display_timezone: ZoneInfo,
) -> str:
    """Render a successful compensating transaction with its durable timestamp."""

    return (
        "✅ **Transaction reversed**\n"
        "The opposite inventory transaction was applied successfully, restoring the "
        "original stock.\n"
        f"🧾 Reversal transaction ID: `{transaction_id}`\n"
        f"🕒 Reversal time: {_format_timestamp(applied_at, display_timezone)}\n"
        "A corrected replacement is a separate transaction and will still require "
        "confirmation."
    )


def _format_timestamp(value: datetime, display_timezone: ZoneInfo) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Transaction timestamp must include a timezone")
    localized = value.astimezone(display_timezone)
    return f"{localized:%d %b %Y, %I:%M:%S %p} ({display_timezone.key})"
