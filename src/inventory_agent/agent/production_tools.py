"""Organization-scoped inventory tools used by the Telegram agent."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from inventory_agent.agent.models import (
    AttributeValue,
    CatalogVariant,
    InventoryReadArguments,
    ReversalProposalArguments,
    StockProposalArguments,
    TrackingMode,
    TransactionReadArguments,
    TransactionRecord,
)
from inventory_agent.agent.prompt import PROMPT_VERSION
from inventory_agent.agent.repository import AgentReadRepository
from inventory_agent.extraction.schema import ItemReferenceType
from inventory_agent.matching.models import InventoryCandidate
from inventory_agent.matching.repository import InventoryCandidateRepository
from inventory_agent.matching.semantic import SemanticCandidateRepository
from inventory_agent.matching.service import MatchingStrategy
from inventory_agent.proposals.models import (
    ProposalDraft,
    ProposalIntent,
    ProposalLineDraft,
)
from inventory_agent.proposals.repository import ProposalRepository
from inventory_agent.reversals.repository import ReversalRepository

logger = logging.getLogger(__name__)


class AgentCatalogReader(Protocol):
    async def read(
        self,
        *,
        organization_id: UUID,
        location_id: UUID,
        arguments: InventoryReadArguments,
    ) -> tuple[list[CatalogVariant], dict[UUID, InventoryCandidate]]:
        """Return compact grounded variants and their retrieval evidence."""


class GroundedAgentCatalogReader:
    """Compose the existing exact/semantic retrieval with current balances."""

    def __init__(
        self,
        *,
        candidates: InventoryCandidateRepository,
        semantic: SemanticCandidateRepository | None,
        reads: AgentReadRepository,
        strategy: MatchingStrategy,
    ) -> None:
        self._candidates = candidates
        self._semantic = semantic
        self._reads = reads
        self._strategy = strategy

    async def read(
        self,
        *,
        organization_id: UUID,
        location_id: UUID,
        arguments: InventoryReadArguments,
    ) -> tuple[list[CatalogVariant], dict[UUID, InventoryCandidate]]:
        query = arguments.sku or arguments.query or ""
        search_started = perf_counter()
        if arguments.sku is not None:
            candidates = await self._candidates.find_candidates(
                organization_id=organization_id,
                query=arguments.sku,
                reference_type=ItemReferenceType.SKU,
                limit=arguments.limit,
            )
        elif arguments.query and self._strategy is not MatchingStrategy.FUZZY:
            if self._semantic is None:
                raise RuntimeError("Semantic agent reads require a semantic repository")
            candidates = await self._semantic.find_candidates(
                organization_id=organization_id,
                query=arguments.query,
                limit=arguments.limit,
            )
        elif arguments.query:
            candidates = await self._candidates.find_candidates(
                organization_id=organization_id,
                query=arguments.query,
                reference_type=ItemReferenceType.NAME,
                limit=arguments.limit,
            )
        else:
            candidates = await self._candidates.browse_candidates(
                organization_id=organization_id,
                query=query,
                limit=arguments.limit,
            )
        _log_runtime(
            component="catalog_candidate_search",
            started=search_started,
            organization_id=organization_id,
            result_count=len(candidates),
        )

        requested_attributes = {
            attribute.key.casefold(): attribute.value.casefold()
            for attribute in arguments.attributes
        }
        if requested_attributes:
            candidates = [
                candidate
                for candidate in candidates
                if _candidate_has_attributes(candidate, requested_attributes)
            ]
        balances_started = perf_counter()
        balances = await self._reads.get_variant_balances(
            organization_id=organization_id,
            location_id=location_id,
            variant_ids=[candidate.item_variant_id for candidate in candidates],
        )
        _log_runtime(
            component="inventory_balance_read",
            started=balances_started,
            organization_id=organization_id,
            result_count=len(balances),
        )
        records = [
            _catalog_variant(candidate, balances.get(candidate.item_variant_id, Decimal("0")))
            for candidate in candidates
            if arguments.include_zero_stock
            or balances.get(candidate.item_variant_id, Decimal("0")) != 0
        ]
        return records, {candidate.item_variant_id: candidate for candidate in candidates}


@dataclass(frozen=True, slots=True)
class ProductionToolContext:
    organization_id: UUID
    organization_user_id: UUID
    location_id: UUID
    source_event_id: UUID
    external_event_id: str
    chat_id: int


class ProductionInventoryAgentTools:
    """Create reviewable proposals while never applying stock directly."""

    def __init__(
        self,
        *,
        context: ProductionToolContext,
        catalog: AgentCatalogReader,
        reads: AgentReadRepository,
        proposals: ProposalRepository,
        reversals: ReversalRepository,
        allowed_variant_ids: set[UUID] | None = None,
        preselected_transactions: list[TransactionRecord] | None = None,
    ) -> None:
        self._context = context
        self._catalog = catalog
        self._reads = reads
        self._proposals = proposals
        self._reversals = reversals
        self.allowed_variant_ids = set(allowed_variant_ids or set())
        self.allowed_transaction_ids: set[UUID] = set()
        self._candidate_evidence: dict[UUID, InventoryCandidate] = {}
        self._transaction_ids_by_ref: dict[str, UUID] = {}
        self._transaction_refs_by_id: dict[UUID, str] = {}
        for transaction in preselected_transactions or []:
            self._register_transaction(transaction)
        self._results_by_call_id: dict[str, str] = {}
        self.stock_proposal_id: UUID | None = None
        self.reversal_request_id: UUID | None = None
        self.reversal_reason: str | None = None

    async def execute(
        self,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, object],
    ) -> str:
        if call_id in self._results_by_call_id:
            return self._results_by_call_id[call_id]
        started = perf_counter()
        try:
            result = await self._dispatch(call_id=call_id, name=name, arguments=arguments)
        except (ValueError, ValidationError) as error:
            result = {"ok": False, "error": str(error)}
        output = json.dumps(result, separators=(",", ":"), default=str)
        self._results_by_call_id[call_id] = output
        _log_runtime(
            component=f"agent_tool.{name}",
            started=started,
            organization_id=self._context.organization_id,
            source_event_id=self._context.source_event_id,
            ok=bool(result.get("ok")),
        )
        return output

    async def _dispatch(
        self,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        if name == "read_inventory":
            return await self._read_inventory(InventoryReadArguments.model_validate(arguments))
        if name == "propose_add_inventory":
            return await self._propose_stock(
                call_id=call_id,
                intent=ProposalIntent.RECEIVE_STOCK,
                arguments=StockProposalArguments.model_validate(arguments),
            )
        if name == "propose_deduct_inventory":
            return await self._propose_stock(
                call_id=call_id,
                intent=ProposalIntent.ISSUE_STOCK,
                arguments=StockProposalArguments.model_validate(arguments),
            )
        if name == "read_transactions":
            return await self._read_transactions(TransactionReadArguments.model_validate(arguments))
        if name == "propose_reversal":
            return await self._propose_reversal(
                call_id=call_id,
                arguments=ReversalProposalArguments.model_validate(arguments),
            )
        return {"ok": False, "error": f"unknown tool: {name}"}

    async def _read_inventory(
        self,
        arguments: InventoryReadArguments,
    ) -> dict[str, object]:
        records, evidence = await self._catalog.read(
            organization_id=self._context.organization_id,
            location_id=self._context.location_id,
            arguments=arguments,
        )
        self.allowed_variant_ids.update(UUID(record.variant_id) for record in records)
        self._candidate_evidence.update(evidence)
        items: list[dict[str, object]] = []
        for record in records:
            item = record.model_dump(mode="json")
            candidate = evidence.get(UUID(record.variant_id))
            if candidate is not None:
                item["match_method"] = candidate.match_method.value
                item["match_score"] = str(candidate.match_score)
            items.append(item)
        return {
            "ok": True,
            "count": len(records),
            "result_scope": (
                "catalog_listing"
                if arguments.query is None and arguments.sku is None
                else "ranked_candidates"
            ),
            "query": arguments.query,
            "sku": arguments.sku,
            "items": items,
            "has_more": len(records) == arguments.limit,
            "aggregation_guidance": (
                "For a category-wide quantity or total, inspect every ranked candidate, "
                "include every genuinely relevant variant, exclude incidental candidates, "
                "and report a per-variant breakdown plus the total."
            ),
        }

    async def _propose_stock(
        self,
        *,
        call_id: str,
        intent: ProposalIntent,
        arguments: StockProposalArguments,
    ) -> dict[str, object]:
        if self.stock_proposal_id is not None or self.reversal_request_id is not None:
            raise ValueError("only one mutation proposal is allowed per user message")
        self.stock_proposal_id = await self._create_stock_proposal(
            call_id=call_id,
            intent=intent,
            arguments=arguments,
            idempotency_suffix="inventory-agent",
        )
        return {
            "ok": True,
            "proposal_id": str(self.stock_proposal_id),
            "operation": "ADD" if intent is ProposalIntent.RECEIVE_STOCK else "DEDUCT",
            "inventory_changed": False,
            "confirmation_required": True,
        }

    async def _create_stock_proposal(
        self,
        *,
        call_id: str,
        intent: ProposalIntent,
        arguments: StockProposalArguments,
        idempotency_suffix: str,
        existing_variants_only: bool = False,
    ) -> UUID:
        lines: list[ProposalLineDraft] = []
        raw_lines: list[dict[str, object]] = []
        for index, line in enumerate(arguments.lines, start=1):
            if intent is ProposalIntent.ISSUE_STOCK and line.new_item is not None:
                raise ValueError("deductions cannot create catalog items")
            if existing_variants_only and line.new_item is not None:
                raise ValueError("a correction replacement must use existing catalog variants")
            item_variant_id = UUID(line.variant_id) if line.variant_id is not None else None
            if item_variant_id is not None and item_variant_id not in self.allowed_variant_ids:
                raise ValueError(
                    f"variant_id {line.variant_id!r} was not returned by read_inventory"
                )
            new_item = line.new_item
            if new_item is not None and new_item.tracking_mode is not TrackingMode.SIMPLE:
                raise ValueError("the prototype currently supports simple tracking only")
            candidate = (
                self._candidate_evidence.get(item_variant_id)
                if item_variant_id is not None
                else None
            )
            if item_variant_id is not None and candidate is None:
                raise ValueError(
                    "read_inventory must return this variant during the current user message"
                )
            description = (
                candidate.display_name
                if candidate is not None
                else new_item.name
                if new_item is not None
                else line.variant_id
            )
            attributes = {attribute.key: attribute.value for attribute in line.attributes}
            evidence: dict[str, object]
            if item_variant_id is not None:
                evidence = {
                    "decision": "matched",
                    "source": "inventory_agent_tool",
                    "candidate": (
                        candidate.model_dump(mode="json") if candidate is not None else None
                    ),
                }
            else:
                evidence = {
                    "decision": "not_found",
                    "source": "inventory_agent_tool",
                    "new_item": new_item.model_dump(mode="json") if new_item else None,
                    "candidates": [],
                }
            lines.append(
                ProposalLineDraft(
                    line_number=index,
                    source_text=description or "inventory item",
                    extracted_description=description,
                    requested_quantity=line.quantity,
                    requested_unit=line.unit,
                    item_variant_id=item_variant_id,
                    match_method=candidate.match_method if candidate else None,
                    match_score=candidate.match_score if candidate else None,
                    match_evidence=evidence,
                    attributes=attributes,
                )
            )
            reference_value = (
                new_item.sku
                if new_item is not None and new_item.sku
                else new_item.name
                if new_item is not None
                else candidate.sku
                if candidate is not None
                else line.variant_id
            )
            raw_lines.append(
                {
                    "item_reference": {
                        "type": (
                            "SKU"
                            if (new_item is not None and new_item.sku) or candidate is not None
                            else "NAME"
                        ),
                        "value": reference_value,
                    },
                    "description": description,
                    "quantity": str(line.quantity),
                    "unit": line.unit,
                    "attributes": attributes,
                }
            )
        return await self._proposals.create(
            ProposalDraft(
                organization_id=self._context.organization_id,
                location_id=self._context.location_id,
                source_event_id=self._context.source_event_id,
                created_by=self._context.organization_user_id,
                intent=intent,
                idempotency_key=(
                    f"telegram:{self._context.external_event_id}:{idempotency_suffix}"
                ),
                raw_command={
                    "schema_version": "inventory-agent-v1",
                    "tool_call_id": call_id,
                    "lines": raw_lines,
                },
                prompt_version=PROMPT_VERSION,
                notes=arguments.reason,
                lines=lines,
            )
        )

    async def _read_transactions(
        self,
        arguments: TransactionReadArguments,
    ) -> dict[str, object]:
        exact_id_lookup = _transaction_id_query(arguments.query)
        targeted = await self._reads.read_transactions(
            organization_id=self._context.organization_id,
            query=arguments.query,
            limit=arguments.limit,
        )
        transactions = targeted
        included_recent_fallback = False
        if arguments.query is not None and arguments.query.strip() and exact_id_lookup is None:
            recent = await self._reads.read_transactions(
                organization_id=self._context.organization_id,
                query=None,
                limit=arguments.limit,
            )
            seen = {transaction.transaction_id for transaction in targeted}
            transactions = [
                *targeted,
                *(transaction for transaction in recent if transaction.transaction_id not in seen),
            ][: arguments.limit]
            included_recent_fallback = len(transactions) > len(targeted)
        self.allowed_transaction_ids.update(
            UUID(transaction.transaction_id) for transaction in transactions
        )
        serialized_transactions = []
        for transaction in transactions:
            serialized_transactions.append(
                {
                    **transaction.model_dump(mode="json"),
                    "transaction_ref": self._register_transaction(transaction),
                }
            )
        return {
            "ok": True,
            "count": len(transactions),
            "targeted_count": len(targeted),
            "exact_id_lookup": exact_id_lookup is not None,
            "included_recent_fallback": included_recent_fallback,
            "transactions": serialized_transactions,
        }

    async def _propose_reversal(
        self,
        *,
        call_id: str,
        arguments: ReversalProposalArguments,
    ) -> dict[str, object]:
        if self.stock_proposal_id is not None or self.reversal_request_id is not None:
            raise ValueError("only one mutation proposal is allowed per user message")
        transaction_id = self._transaction_ids_by_ref.get(arguments.transaction_ref)
        if transaction_id is None:
            raise ValueError(
                "transaction_ref was not returned by read_transactions during this user message"
            )
        request_id = await self._reversals.begin(
            transaction_id=transaction_id,
            actor_id=self._context.organization_user_id,
            chat_id=self._context.chat_id,
        )
        captured = await self._reversals.capture_reason(
            event_id=self._context.source_event_id,
            actor_id=self._context.organization_user_id,
            chat_id=self._context.chat_id,
            reason=arguments.reason,
        )
        if captured is None or captured != request_id:
            raise ValueError("reversal reason could not be attached to the request")
        replacement_proposal_id: UUID | None = None
        if arguments.replacement is not None:
            replacement_intent = (
                ProposalIntent.RECEIVE_STOCK
                if arguments.replacement.operation == "ADD"
                else ProposalIntent.ISSUE_STOCK
            )
            replacement_proposal_id = await self._create_stock_proposal(
                call_id=call_id,
                intent=replacement_intent,
                arguments=StockProposalArguments(
                    lines=arguments.replacement.lines,
                    reason=arguments.replacement.reason,
                ),
                idempotency_suffix="inventory-agent-correction-replacement",
                existing_variants_only=True,
            )
            await self._reversals.attach_replacement(
                request_id=request_id,
                proposal_id=replacement_proposal_id,
                actor_id=self._context.organization_user_id,
            )
        self.reversal_request_id = request_id
        self.reversal_reason = arguments.reason
        return {
            "ok": True,
            "proposal_id": str(request_id),
            "operation": "REVERSE",
            "replacement_proposal_id": (
                str(replacement_proposal_id) if replacement_proposal_id else None
            ),
            "replacement_will_follow_automatically": replacement_proposal_id is not None,
            "inventory_changed": False,
            "confirmation_required": True,
        }

    def _register_transaction(self, transaction: TransactionRecord) -> str:
        transaction_id = UUID(transaction.transaction_id)
        existing = self._transaction_refs_by_id.get(transaction_id)
        if existing is not None:
            return existing
        transaction_ref = f"T{len(self._transaction_ids_by_ref) + 1}"
        self._transaction_ids_by_ref[transaction_ref] = transaction_id
        self._transaction_refs_by_id[transaction_id] = transaction_ref
        self.allowed_transaction_ids.add(transaction_id)
        return transaction_ref


def _candidate_has_attributes(
    candidate: InventoryCandidate,
    requested: dict[str, str],
) -> bool:
    available = {
        str(key).casefold(): str(value).casefold()
        for key, value in {
            **candidate.item_attributes,
            **candidate.variant_attributes,
        }.items()
    }
    return all(available.get(key) == value for key, value in requested.items())


def _transaction_id_query(query: str | None) -> UUID | None:
    if query is None:
        return None
    try:
        return UUID(query.strip())
    except ValueError:
        return None


def _catalog_variant(candidate: InventoryCandidate, on_hand: Decimal) -> CatalogVariant:
    attributes = {
        **candidate.item_attributes,
        **candidate.variant_attributes,
    }
    return CatalogVariant(
        variant_id=str(candidate.item_variant_id),
        item_name=candidate.item_name,
        variant_name=candidate.variant_name,
        sku=candidate.sku,
        base_unit=candidate.base_unit,
        tracking_mode=TrackingMode(candidate.tracking_mode.value),
        attributes=[
            AttributeValue(key=str(key), value=str(value))
            for key, value in sorted(attributes.items())
            if value is not None
        ],
        on_hand=on_hand,
    )


def _log_runtime(
    *,
    component: str,
    started: float,
    organization_id: UUID,
    **fields: object,
) -> None:
    details = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    logger.info(
        "component_runtime component=%s duration_ms=%.2f organization_id=%s%s",
        component,
        (perf_counter() - started) * 1000,
        organization_id,
        f" {details}" if details else "",
    )
