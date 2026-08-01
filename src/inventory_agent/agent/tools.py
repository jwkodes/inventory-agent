"""Strict tool definitions and a no-write in-memory inventory implementation."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Final

from pydantic import ValidationError

from inventory_agent.agent.models import (
    CatalogItemEditArguments,
    CatalogVariant,
    InventoryReadArguments,
    ReversalProposalArguments,
    SimulationProposal,
    StockProposalArguments,
    TrackingMode,
    TransactionReadArguments,
    TransactionRecord,
)


def _nullable(schema_type: str) -> list[str]:
    return [schema_type, "null"]


ATTRIBUTE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "minLength": 1},
        "value": {"type": "string", "minLength": 1},
    },
    "required": ["key", "value"],
    "additionalProperties": False,
}

ATTRIBUTE_CHANGE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "minLength": 1, "maxLength": 100},
        "value": {"type": ["string", "null"], "maxLength": 1000},
    },
    "required": ["key", "value"],
    "additionalProperties": False,
}

NEW_ITEM_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "sku": {
            "type": _nullable("string"),
            "description": (
                "SKU or internal product code. Prompt for it once when missing. Use null "
                "only after the user explicitly says to create the item without an SKU for "
                "now; it can be assigned later through a catalog metadata update."
            ),
        },
        "sku_deferred": {
            "type": "boolean",
            "description": (
                "True only when the user explicitly said there is no SKU for now, to skip "
                "or ignore it, or to assign it later. Otherwise false."
            ),
        },
        "base_unit": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Canonical stock-counting unit. Silently use 'each' for an individually "
                "counted physical item; unit, units, item, and items mean the same thing. "
                "Use a package or measurement unit only when it is materially specified."
            ),
        },
        "tracking_mode": {"type": "string", "enum": ["simple"]},
        "attributes": {"type": "array", "items": ATTRIBUTE_SCHEMA},
    },
    "required": [
        "name",
        "sku",
        "sku_deferred",
        "base_unit",
        "tracking_mode",
        "attributes",
    ],
    "additionalProperties": False,
}

STOCK_LINE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "variant_id": {"type": _nullable("string")},
        "new_item": {"anyOf": [NEW_ITEM_SCHEMA, {"type": "null"}]},
        "quantity": {"type": "number", "exclusiveMinimum": 0},
        "unit": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Unit used by this quantity. Generic unit/item wording means one counted "
                "SKU and does not require user clarification."
            ),
        },
        "attributes": {"type": "array", "items": ATTRIBUTE_SCHEMA},
    },
    "required": ["variant_id", "new_item", "quantity", "unit", "attributes"],
    "additionalProperties": False,
}

INVENTORY_TOOL_DEFINITIONS: Final[list[dict[str, object]]] = [
    {
        "type": "function",
        "name": "read_inventory",
        "description": (
            "Search or browse this company's catalog and current on-hand balances. "
            "Use null query and null SKU only when a broad inventory listing is necessary. "
            "Query results are ranked candidates and can include incidental items; each "
            "item includes its match method and score. For category-wide totals, request "
            "limit 50 and inspect every candidate rather than using only the first result. "
            "Returns authoritative variant IDs that may be used in proposal tools."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": _nullable("string")},
                "sku": {"type": _nullable("string")},
                "attributes": {"type": "array", "items": ATTRIBUTE_SCHEMA},
                "include_zero_stock": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query", "sku", "attributes", "include_zero_stock", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "propose_add_inventory",
        "description": (
            "Create a no-write proposal to receive stock. Existing variant IDs must come "
            "from read_inventory. A new_item is allowed only after the user explicitly "
            "agrees to add a new catalog item. Prompt for its SKU/internal code once when "
            "missing, but use null if the user explicitly defers it. Preserve "
            "user-provided custom fields in "
            "new_item.attributes; attributes are optional unless a tool explicitly says "
            "the organization requires them. This tool never changes inventory."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "lines": {"type": "array", "minItems": 1, "items": STOCK_LINE_SCHEMA},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["lines", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "propose_deduct_inventory",
        "description": (
            "Create a no-write proposal to deduct stock from existing variants. Variant IDs "
            "must come from read_inventory and new_item must be null. This tool never "
            "changes inventory."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "lines": {"type": "array", "minItems": 1, "items": STOCK_LINE_SCHEMA},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["lines", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_transactions",
        "description": (
            "Search recent immutable inventory transactions before proposing a reversal. "
            "A full transaction UUID performs an exact lookup. Returns authoritative "
            "transaction_ref values for use by propose_reversal, plus display-only UUIDs, "
            "the stored transaction_type, stored status, timestamp, summary, and derived "
            "reversed flag. A natural-language filtered search also includes recent "
            "transactions as fallback evidence so wording cannot hide an existing "
            "transaction. References are valid only during the current user message."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": _nullable("string")},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "propose_catalog_update",
        "description": (
            "Create a no-write, manager/admin-reviewed request to update catalog metadata "
            "for one variant returned by read_inventory during this user message. Supported "
            "fields are item name, variant name, SKU, description, item attributes, and "
            "variant attributes. A null attribute value removes that attribute. Use "
            "clear_fields only to clear variant_name or description. This tool never changes "
            "stock, ledger rows, base unit, or tracking mode."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "variant_id": {"type": "string"},
                "item_name": {"type": _nullable("string"), "maxLength": 200},
                "variant_name": {"type": _nullable("string"), "maxLength": 200},
                "sku": {"type": _nullable("string"), "maxLength": 100},
                "description": {"type": _nullable("string"), "maxLength": 2000},
                "clear_fields": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["variant_name", "description"]},
                },
                "item_attribute_changes": {
                    "type": "array",
                    "items": ATTRIBUTE_CHANGE_SCHEMA,
                },
                "variant_attribute_changes": {
                    "type": "array",
                    "items": ATTRIBUTE_CHANGE_SCHEMA,
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            },
            "required": [
                "variant_id",
                "item_name",
                "variant_name",
                "sku",
                "description",
                "clear_fields",
                "item_attribute_changes",
                "variant_attribute_changes",
                "reason",
            ],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "propose_reversal",
        "description": (
            "Create a no-write compensating reversal proposal for a transaction returned by "
            "read_transactions during the current user message. Use its short transaction_ref, "
            "never copy or reconstruct the display-only transaction UUID. For a correction "
            "whose replacement quantities are already known, include a grounded replacement "
            "so its separate confirmation appears automatically after the reversal succeeds. "
            "This never deletes history or changes inventory."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "transaction_ref": {
                    "type": "string",
                    "pattern": "^T[1-9][0-9]*$",
                    "description": "Current-turn reference returned by read_transactions.",
                },
                "reason": {"type": "string", "minLength": 1},
                "replacement": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "operation": {"type": "string", "enum": ["ADD", "DEDUCT"]},
                                "lines": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": STOCK_LINE_SCHEMA,
                                },
                                "reason": {"type": "string", "minLength": 1},
                            },
                            "required": ["operation", "lines", "reason"],
                            "additionalProperties": False,
                        },
                        {"type": "null"},
                    ]
                },
            },
            "required": ["transaction_ref", "reason", "replacement"],
            "additionalProperties": False,
        },
    },
]


class SimulatedInventoryTools:
    """Execute inventory reads and record proposals without changing balances."""

    def __init__(
        self,
        *,
        catalog: list[CatalogVariant],
        transactions: list[TransactionRecord] | None = None,
    ) -> None:
        self._catalog = catalog
        self._transactions = transactions or []
        self._seen_variant_ids: set[str] = set()
        self._transaction_ids_by_ref: dict[str, str] = {}
        self._transaction_refs_by_id: dict[str, str] = {}
        self._results_by_call_id: dict[str, str] = {}
        self.proposals: list[SimulationProposal] = []
        self.catalog_edits: list[SimulationProposal] = []

    async def execute(
        self,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, object],
    ) -> str:
        """Validate and execute one idempotent simulated function call."""

        if call_id in self._results_by_call_id:
            return self._results_by_call_id[call_id]
        try:
            result = self._dispatch(name=name, arguments=arguments)
        except (ValueError, ValidationError) as error:
            result = {"ok": False, "error": str(error)}
        output = json.dumps(result, separators=(",", ":"), default=str)
        self._results_by_call_id[call_id] = output
        return output

    def _dispatch(self, *, name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "read_inventory":
            return self._read_inventory(InventoryReadArguments.model_validate(arguments))
        if name == "propose_add_inventory":
            return self._propose_stock(
                operation="ADD",
                arguments=StockProposalArguments.model_validate(arguments),
            )
        if name == "propose_deduct_inventory":
            return self._propose_stock(
                operation="DEDUCT",
                arguments=StockProposalArguments.model_validate(arguments),
            )
        if name == "read_transactions":
            return self._read_transactions(TransactionReadArguments.model_validate(arguments))
        if name == "propose_catalog_update":
            return self._propose_catalog_update(CatalogItemEditArguments.model_validate(arguments))
        if name == "propose_reversal":
            return self._propose_reversal(ReversalProposalArguments.model_validate(arguments))
        return {"ok": False, "error": f"unknown tool: {name}"}

    def _propose_catalog_update(self, arguments: CatalogItemEditArguments) -> dict[str, object]:
        if arguments.variant_id not in self._seen_variant_ids:
            raise ValueError(
                f"variant_id {arguments.variant_id!r} was not returned by read_inventory"
            )
        proposal = SimulationProposal(
            proposal_id=f"sim-catalog-edit-{len(self.catalog_edits) + 1}",
            operation="CATALOG_UPDATE",
            payload=arguments.model_dump(mode="json"),
        )
        self.catalog_edits.append(proposal)
        return {
            "ok": True,
            "catalog_edit_request_id": proposal.proposal_id,
            "catalog_changed": False,
            "inventory_changed": False,
            "confirmation_required": True,
        }

    def _read_inventory(self, arguments: InventoryReadArguments) -> dict[str, object]:
        requested_attributes = {
            attribute.key.casefold(): attribute.value.casefold()
            for attribute in arguments.attributes
        }
        candidates = [
            variant
            for variant in self._catalog
            if arguments.include_zero_stock or variant.on_hand != 0
        ]
        if arguments.sku:
            sku = arguments.sku.casefold()
            candidates = [
                candidate
                for candidate in candidates
                if candidate.sku is not None and candidate.sku.casefold() == sku
            ]
        if requested_attributes:
            candidates = [
                candidate
                for candidate in candidates
                if _attributes_include(candidate, requested_attributes)
            ]
        if arguments.query:
            candidates = sorted(
                candidates,
                key=lambda candidate: _catalog_score(candidate, arguments.query or ""),
                reverse=True,
            )
            candidates = [
                candidate
                for candidate in candidates
                if _catalog_score(candidate, arguments.query or "") >= 0.2
            ]
        selected = candidates[: arguments.limit]
        self._seen_variant_ids.update(candidate.variant_id for candidate in selected)
        return {
            "ok": True,
            "count": len(selected),
            "items": [candidate.model_dump(mode="json") for candidate in selected],
            "has_more": len(candidates) > len(selected),
        }

    def _propose_stock(
        self,
        *,
        operation: str,
        arguments: StockProposalArguments,
    ) -> dict[str, object]:
        for line in arguments.lines:
            if operation == "DEDUCT" and line.new_item is not None:
                raise ValueError("deductions cannot create catalog items")
            if line.new_item is not None and line.new_item.tracking_mode is not TrackingMode.SIMPLE:
                raise ValueError("the prototype currently supports simple tracking only")
        missing_sku_items = [
            line.new_item.name
            for line in arguments.lines
            if line.new_item is not None
            and line.new_item.sku is None
            and not line.new_item.sku_deferred
        ]
        if missing_sku_items:
            return new_item_sku_required_result(missing_sku_items)
        for line in arguments.lines:
            if line.variant_id is not None and line.variant_id not in self._seen_variant_ids:
                raise ValueError(
                    f"variant_id {line.variant_id!r} was not returned by read_inventory"
                )
        proposal = SimulationProposal(
            proposal_id=f"sim-proposal-{len(self.proposals) + 1}",
            operation=operation,
            payload=arguments.model_dump(mode="json"),
        )
        self.proposals.append(proposal)
        return {
            "ok": True,
            "proposal": proposal.model_dump(mode="json"),
            "inventory_changed": False,
            "confirmation_required": True,
        }

    def _read_transactions(self, arguments: TransactionReadArguments) -> dict[str, object]:
        transactions = self._transactions
        if arguments.query:
            query = _normalized(arguments.query)
            transactions = [
                transaction
                for transaction in transactions
                if query
                in _normalized(
                    f"{transaction.transaction_id} {transaction.summary} "
                    f"{transaction.transaction_type}"
                )
                or SequenceMatcher(
                    None,
                    query,
                    _normalized(transaction.summary),
                ).ratio()
                >= 0.3
            ]
        selected = transactions[: arguments.limit]
        serialized = []
        for transaction in selected:
            transaction_ref = self._transaction_ref(transaction.transaction_id)
            serialized.append(
                {
                    **transaction.model_dump(mode="json"),
                    "transaction_ref": transaction_ref,
                }
            )
        return {
            "ok": True,
            "count": len(selected),
            "transactions": serialized,
        }

    def _propose_reversal(
        self,
        arguments: ReversalProposalArguments,
    ) -> dict[str, object]:
        transaction_id = self._transaction_ids_by_ref.get(arguments.transaction_ref)
        if transaction_id is None:
            raise ValueError(
                "transaction_ref was not returned by read_transactions during this user message"
            )
        transaction = next(
            (record for record in self._transactions if record.transaction_id == transaction_id),
            None,
        )
        if transaction is None:
            raise ValueError("transaction does not exist")
        if transaction.reversed:
            raise ValueError("transaction has already been reversed")
        proposal = SimulationProposal(
            proposal_id=f"sim-proposal-{len(self.proposals) + 1}",
            operation="REVERSE",
            payload=arguments.model_dump(mode="json"),
        )
        self.proposals.append(proposal)
        return {
            "ok": True,
            "proposal": proposal.model_dump(mode="json"),
            "inventory_changed": False,
            "confirmation_required": True,
        }

    def _transaction_ref(self, transaction_id: str) -> str:
        existing = self._transaction_refs_by_id.get(transaction_id)
        if existing is not None:
            return existing
        transaction_ref = f"T{len(self._transaction_ids_by_ref) + 1}"
        self._transaction_ids_by_ref[transaction_ref] = transaction_id
        self._transaction_refs_by_id[transaction_id] = transaction_ref
        return transaction_ref


def new_item_sku_required_result(item_names: list[str]) -> dict[str, object]:
    """Prompt once unless the user explicitly chose to assign the SKU later."""

    names = [name.strip() for name in item_names if name.strip()]
    listed_names = ", ".join(names)
    return {
        "ok": False,
        "error_code": "new_item_sku_required",
        "error": "Ask for an SKU once, or set sku_deferred after an explicit opt-out.",
        "requires_user_input": True,
        "user_message": (
            f"What SKU or internal product code should I use for {listed_names}? "
            "If it does not have one yet, you can say **no SKU for now**."
        ),
    }


def _attributes_include(
    variant: CatalogVariant,
    requested: dict[str, str],
) -> bool:
    available = {
        attribute.key.casefold(): attribute.value.casefold() for attribute in variant.attributes
    }
    return all(available.get(key) == value for key, value in requested.items())


def _catalog_score(variant: CatalogVariant, query: str) -> float:
    query_normalized = _normalized(query)
    candidate = _normalized(
        " ".join(
            [
                variant.item_name,
                variant.variant_name or "",
                variant.sku or "",
                *(f"{attribute.key} {attribute.value}" for attribute in variant.attributes),
            ]
        )
    )
    if not query_normalized:
        return 1.0
    query_tokens = set(query_normalized.split())
    candidate_tokens = set(candidate.split())
    overlap = len(query_tokens & candidate_tokens) / max(len(query_tokens), 1)
    sequence = SequenceMatcher(None, query_normalized, candidate).ratio()
    if not query_tokens.intersection(candidate_tokens) and sequence < 0.55:
        return 0.0
    return max(overlap, sequence)


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))
