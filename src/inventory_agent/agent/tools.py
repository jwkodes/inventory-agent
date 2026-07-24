"""Strict tool definitions and a no-write in-memory inventory implementation."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Final

from pydantic import ValidationError

from inventory_agent.agent.models import (
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

NEW_ITEM_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "sku": {"type": _nullable("string")},
        "base_unit": {"type": "string", "minLength": 1},
        "tracking_mode": {"type": "string", "enum": ["simple"]},
        "attributes": {"type": "array", "items": ATTRIBUTE_SCHEMA},
    },
    "required": ["name", "sku", "base_unit", "tracking_mode", "attributes"],
    "additionalProperties": False,
}

STOCK_LINE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "variant_id": {"type": _nullable("string")},
        "new_item": {"anyOf": [NEW_ITEM_SCHEMA, {"type": "null"}]},
        "quantity": {"type": "number", "exclusiveMinimum": 0},
        "unit": {"type": "string", "minLength": 1},
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
            "agrees to add a new catalog item. This tool never changes inventory."
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
            "Returns authoritative transaction IDs."
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
        "name": "propose_reversal",
        "description": (
            "Create a no-write compensating reversal proposal for a transaction returned by "
            "read_transactions. This never deletes history or changes inventory."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["transaction_id", "reason"],
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
        self._seen_transaction_ids: set[str] = set()
        self._results_by_call_id: dict[str, str] = {}
        self.proposals: list[SimulationProposal] = []

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
        if name == "propose_reversal":
            return self._propose_reversal(ReversalProposalArguments.model_validate(arguments))
        return {"ok": False, "error": f"unknown tool: {name}"}

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
        self._seen_transaction_ids.update(transaction.transaction_id for transaction in selected)
        return {
            "ok": True,
            "count": len(selected),
            "transactions": [transaction.model_dump(mode="json") for transaction in selected],
        }

    def _propose_reversal(
        self,
        arguments: ReversalProposalArguments,
    ) -> dict[str, object]:
        if arguments.transaction_id not in self._seen_transaction_ids:
            raise ValueError("transaction_id was not returned by read_transactions in this session")
        transaction = next(
            (
                record
                for record in self._transactions
                if record.transaction_id == arguments.transaction_id
            ),
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
