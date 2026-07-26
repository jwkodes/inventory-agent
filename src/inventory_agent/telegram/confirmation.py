"""Render proposal review messages and inline keyboards."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from inventory_agent.catalog.models import CatalogBatchCreationView, CatalogItemCreationView
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
    user_resolution: str | None = None


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
    active_lines = [line for line in lines if line.user_resolution != "ignored"]
    has_multiple_unresolved_lines = (
        sum(line.matched_label is None and line.user_resolution != "ignored" for line in lines) > 1
    )
    pending_new_lines = [
        line
        for line in lines
        if line.matched_label is None
        and line.match_decision == "not_found"
        and not line.show_candidates
        and line.user_resolution is None
    ]
    selected_new_lines = [
        line for line in lines if line.matched_label is None and line.user_resolution == "add_new"
    ]
    new_item_workflow_lines = [*selected_new_lines, *pending_new_lines]
    bulk_new_items = len(new_item_workflow_lines) > 1
    first_pending_new_id = pending_new_lines[0].proposal_line_id if pending_new_lines else None
    pending_new_indices = [
        index for index, line in enumerate(lines, start=1) if line in pending_new_lines
    ]
    selected_new_indices = [
        index for index, line in enumerate(lines, start=1) if line in selected_new_lines
    ]
    candidate_resolution_pending = any(
        line.matched_label is None and line.show_candidates and line.user_resolution != "ignored"
        for line in lines
    )

    for index, line in enumerate(lines, start=1):
        unit = f" {line.unit}" if line.unit else ""
        quantity = format(line.quantity.normalize(), "f")
        if line.user_resolution == "ignored":
            text_lines.append(
                f"{index}. 🚫 IGNORE {quantity}{unit} — {line.description} → excluded"
            )
            continue
        if line.user_resolution == "add_new" and line.matched_label is None:
            text_lines.append(
                f"{index}. 🆕 ADD AS NEW {quantity}{unit} — {line.description} → details pending"
            )
            unresolved = True
            continue
        match = line.matched_label or _unresolved_status(line)
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
                subject = f"line {index}" if len(lines) > 1 else "this item"
                if not bulk_new_items:
                    text_lines.append(
                        f"🔎 **No confident match:** No catalog match was found for {subject}."
                    )
                if not bulk_new_items:
                    keyboard.append(
                        [
                            InlineButton(
                                text="Add new item",
                                callback_data=encode_callback(
                                    CallbackCommand(
                                        CallbackAction.ADD_NEW_ITEM,
                                        line.proposal_line_id,
                                    )
                                ),
                            ),
                            InlineButton(
                                text=(
                                    f"Match line {index}" if bulk_new_items else "Choose existing"
                                ),
                                callback_data=encode_callback(
                                    CallbackCommand(
                                        CallbackAction.SHOW_EXISTING_ITEMS,
                                        line.proposal_line_id,
                                    )
                                ),
                            ),
                            *(
                                [
                                    InlineButton(
                                        text=f"Ignore line {index}",
                                        callback_data=encode_callback(
                                            CallbackCommand(
                                                CallbackAction.IGNORE_PROPOSAL_LINE,
                                                line.proposal_line_id,
                                            )
                                        ),
                                    )
                                ]
                                if len(active_lines) > 1
                                else []
                            ),
                        ]
                    )
                elif (
                    line.proposal_line_id == first_pending_new_id
                    and not candidate_resolution_pending
                ):
                    keyboard.append(
                        [
                            InlineButton(
                                text=f"Add line {index}",
                                callback_data=encode_callback(
                                    CallbackCommand(
                                        CallbackAction.MARK_NEW_ITEM,
                                        line.proposal_line_id,
                                    )
                                ),
                            ),
                            InlineButton(
                                text=f"Match line {index}",
                                callback_data=encode_callback(
                                    CallbackCommand(
                                        CallbackAction.SHOW_EXISTING_ITEMS,
                                        line.proposal_line_id,
                                    )
                                ),
                            ),
                        ]
                    )
                    if len(active_lines) > 1:
                        keyboard.append(
                            [
                                InlineButton(
                                    text=f"Ignore line {index}",
                                    callback_data=encode_callback(
                                        CallbackCommand(
                                            CallbackAction.IGNORE_PROPOSAL_LINE,
                                            line.proposal_line_id,
                                        )
                                    ),
                                )
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
                if line.show_candidates and len(active_lines) > 1:
                    keyboard.append(
                        [
                            InlineButton(
                                text=f"Ignore line {index}",
                                callback_data=encode_callback(
                                    CallbackCommand(
                                        CallbackAction.IGNORE_PROPOSAL_LINE,
                                        line.proposal_line_id,
                                    )
                                ),
                            )
                        ]
                    )

    if not unresolved:
        text_lines.insert(0, f"⏳ **Pending {operation_label}**")
        text_lines.append("Please review, then choose **Confirm** or **Cancel**.")
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
        if bulk_new_items and pending_new_lines and not candidate_resolution_pending:
            text_lines.append(
                "🔎 **No catalog matches:** "
                f"Lines {', '.join(str(index) for index in pending_new_indices)} "
                "still need a decision."
            )
            text_lines.append(
                f"Resolve line {pending_new_indices[0]} next, or add every remaining "
                "unmatched line as new."
            )
            keyboard.insert(
                0,
                [
                    InlineButton(
                        text=f"Add remaining {len(pending_new_lines)} as new",
                        callback_data=encode_callback(
                            CallbackCommand(CallbackAction.ADD_ALL_NEW_ITEMS, proposal_id)
                        ),
                    )
                ],
            )
        elif selected_new_lines and not pending_new_lines:
            if len(selected_new_lines) > 1:
                text_lines.append(
                    "✅ **Line decisions complete:** "
                    f"Lines {', '.join(str(index) for index in selected_new_indices)} "
                    "will be created as new products."
                )
                keyboard.insert(
                    0,
                    [
                        InlineButton(
                            text=f"Continue with {len(selected_new_lines)} new items",
                            callback_data=encode_callback(
                                CallbackCommand(
                                    CallbackAction.ADD_ALL_NEW_ITEMS,
                                    proposal_id,
                                )
                            ),
                        )
                    ],
                )
            else:
                selected_line = selected_new_lines[0]
                text_lines.append(
                    "✅ **Line decisions complete:** Enter the missing details for the new product."
                )
                keyboard.insert(
                    0,
                    [
                        InlineButton(
                            text=f"Enter details for line {selected_new_indices[0]}",
                            callback_data=encode_callback(
                                CallbackCommand(
                                    CallbackAction.ADD_NEW_ITEM,
                                    selected_line.proposal_line_id,
                                )
                            ),
                        )
                    ],
                )
        else:
            text_lines.append("Resolve the unmatched item, or choose **Cancel**.")
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


def _unresolved_status(line: ProposalLineView) -> str:
    if line.match_decision == "clarification_required":
        return "more information needed"
    if line.match_decision == "not_found" and not line.show_candidates:
        return "no catalog match"
    return "choose a catalog match"


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

    if view.details_reason:
        return ConfirmationMessage(
            text=(
                "⚠️ **Different SKU needed**\n"
                f"{view.details_reason}\n\n"
                "Reply naturally with the different SKU/internal code. "
                "The other item details have been retained."
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

    known_name = view.name or view.suggested_name
    known_sku = view.sku or view.suggested_sku
    known_unit = view.base_unit or view.suggested_base_unit
    missing: list[str] = []
    if not known_name:
        missing.append("the item name")
    if not known_sku:
        missing.append("its SKU, part number, or internal product code")
    if not known_unit:
        missing.append("its package or measurement unit")
    retained = ""
    if view.requested_quantity is not None:
        unit = f" {view.requested_unit}" if view.requested_unit else ""
        retained = (
            f"Receipt line retained: {format(view.requested_quantity.normalize(), 'f')}"
            f"{unit} — {known_name or 'unnamed item'}.\n"
        )
    requested = "\n".join(f"• {field}" for field in missing)
    if missing:
        request_text = (
            "The quantity is already saved. Please send only the missing catalog "
            f"information:\n{requested}\n\n"
            "You may also include useful attributes. Reply naturally in any format."
        )
    else:
        request_text = (
            f"Catalog details retained: {known_name or 'unnamed item'}"
            f"{f' · {known_sku}' if known_sku else ''}. "
            "Reply naturally only if you want to correct or add details."
        )
    return ConfirmationMessage(
        text=(f"📝 **New item details needed**\n{retained}{request_text}"),
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


def render_catalog_batch_details_prompt(view: CatalogBatchCreationView) -> ConfirmationMessage:
    """Ask once for missing identifiers across every selected new product."""

    text_lines = [
        f"📝 **{len(view.items)} new catalog items**",
        "The receipt quantities are retained:",
    ]
    for item in view.items:
        quantity = format(item.requested_quantity.normalize(), "f")
        unit = f" {item.requested_unit}" if item.requested_unit else ""
        name = item.name or item.suggested_name or "Unnamed item"
        sku = item.sku or item.suggested_sku
        suffix = f" — SKU {sku}" if sku else " — SKU needed"
        text_lines.append(f"{item.line_number}. {quantity}{unit} — {name}{suffix}")
    text_lines.extend(
        [
            "",
            "Send missing or corrected SKUs in one natural reply. You can refer to line "
            "numbers, or say **generate unique internal SKUs from the descriptions**.",
            "Quantities will not be changed.",
        ]
    )
    return ConfirmationMessage(
        text="\n".join(text_lines),
        inline_keyboard=[
            [
                InlineButton(
                    text="Cancel batch",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CANCEL_CATALOG_BATCH, view.batch_id)
                    ),
                )
            ]
        ],
    )


def render_catalog_batch_confirmation(
    view: CatalogBatchCreationView,
    *,
    proposal: ProposalConfirmationView | None = None,
) -> ConfirmationMessage:
    """Review catalog creation and the receipt behind one atomic confirmation."""

    text_lines = ["⏳ **Pending catalog and stock addition**"]
    if proposal is None:
        text_lines.append(f"Create and receive {len(view.items)} new products:")
        for item in view.items:
            quantity = format(item.requested_quantity.normalize(), "f")
            unit = f" {item.requested_unit}" if item.requested_unit else ""
            name = item.name or item.suggested_name or "Unnamed item"
            sku = item.sku or item.suggested_sku or "missing"
            text_lines.append(
                f"{item.line_number}. 🆕 CREATE + ADD {quantity}{unit} — {name} · {sku}"
            )
    else:
        item_by_line = {item.line_number: item for item in view.items}
        text_lines.append("Review every proposal line:")
        for index, line in enumerate(proposal.lines, start=1):
            quantity = format(line.quantity.normalize(), "f")
            unit = f" {line.unit}" if line.unit else ""
            new_item = item_by_line.get(index)
            if new_item is not None:
                name = new_item.name or new_item.suggested_name or line.description
                sku = new_item.sku or new_item.suggested_sku or "missing"
                text_lines.append(f"{index}. 🆕 CREATE + ADD {quantity}{unit} — {name} · {sku}")
            elif line.user_resolution == "ignored":
                text_lines.append(f"{index}. 🚫 IGNORE {quantity}{unit} — {line.description}")
            elif line.matched_label is not None:
                text_lines.append(
                    f"{index}. ➕ ADD {quantity}{unit} — {line.description} → {line.matched_label}"
                )
            else:
                raise ValueError("Catalog batch proposal still has an unresolved active line")
    text_lines.append(
        "Confirm once to create the new products and apply the complete stock addition."
    )
    return ConfirmationMessage(
        text="\n".join(text_lines),
        inline_keyboard=[
            [
                InlineButton(
                    text="Confirm all changes",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CONFIRM_CATALOG_BATCH, view.batch_id)
                    ),
                ),
                InlineButton(
                    text="Cancel",
                    callback_data=encode_callback(
                        CallbackCommand(CallbackAction.CANCEL_CATALOG_BATCH, view.batch_id)
                    ),
                ),
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
    text_lines = [
        "⏳ **Pending catalog change**",
        f"Name: {view.name}",
        f"SKU: {view.sku}",
        f"Unit: {view.base_unit}",
    ]
    if view.attributes:
        attributes = ", ".join(f"{key}: {value}" for key, value in sorted(view.attributes.items()))
        text_lines.append(f"Attributes: {attributes}")
    text_lines.append("Please review, then choose **Create item** or **Cancel**.")
    return ConfirmationMessage(
        text="\n".join(text_lines),
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

    heading = _applied_transaction_copy(transaction_type)
    return ConfirmationMessage(
        text=(
            f"{heading}\n"
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


def _applied_transaction_copy(transaction_type: str) -> str:
    if transaction_type == "receive":
        return "✅ **Stock added**"
    if transaction_type == "issue":
        return "✅ **Stock deducted**"
    if transaction_type == "adjustment":
        return "✅ **Stock adjusted**"
    raise ValueError("Applied proposal has an unsupported transaction type")


def render_reversal_reason_prompt(request_id: UUID) -> ConfirmationMessage:
    """Ask for free text while retaining an explicit cancellation path."""

    return ConfirmationMessage(
        text=(
            "❓ **Reversal reason needed**\nReply with the reason, or choose **Cancel reversal**."
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
            "This reverses the entire original transaction.\n"
            f"🧾 Original transaction ID: `{original_transaction_id}`\n"
            "🕒 Original transaction time: "
            f"{_format_timestamp(original_transaction_applied_at, display_timezone)}\n"
            f"Reason: {reason}\n"
            "Please review, then choose **Confirm reversal** or **Cancel**."
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
        "The original stock movement was reversed.\n"
        f"🧾 Reversal transaction ID: `{transaction_id}`\n"
        f"🕒 Reversal time: {_format_timestamp(applied_at, display_timezone)}"
    )


def _format_timestamp(value: datetime, display_timezone: ZoneInfo) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Transaction timestamp must include a timezone")
    localized = value.astimezone(display_timezone)
    return f"{localized:%d %b %Y, %I:%M:%S %p} ({display_timezone.key})"
