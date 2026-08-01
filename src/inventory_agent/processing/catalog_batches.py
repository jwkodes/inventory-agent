"""Resume one durable multi-item catalog request from a natural-language reply."""

from __future__ import annotations

from typing import Protocol

from inventory_agent.catalog.batch import (
    CatalogBatchExtractionResult,
    merge_catalog_batch_details,
)
from inventory_agent.catalog.models import CatalogBatchCreationView
from inventory_agent.catalog.repository import CatalogItemCreationRepository
from inventory_agent.catalog.sku import is_explicit_sku_deferral
from inventory_agent.processing.models import (
    ProcessingOutcomeDraft,
    ProcessingOutcomeType,
    TelegramTextEventContext,
    TextEventProcessingResult,
    TextEventProcessingStatus,
)
from inventory_agent.processing.repository import ProcessingOutboxRepository


class CatalogBatchDetailsInterpreter(Protocol):
    async def interpret(
        self,
        *,
        user_text: str,
        view: CatalogBatchCreationView,
    ) -> CatalogBatchExtractionResult:
        """Extract details for any numbered items addressed by a natural reply."""


class CatalogBatchReplyHandler:
    """Keep a pending multi-line catalog form out of the conversational agent."""

    def __init__(
        self,
        *,
        catalog: CatalogItemCreationRepository,
        interpreter: CatalogBatchDetailsInterpreter,
        outbox: ProcessingOutboxRepository,
    ) -> None:
        self._catalog = catalog
        self._interpreter = interpreter
        self._outbox = outbox

    async def handle_pending(
        self,
        *,
        context: TelegramTextEventContext,
    ) -> TextEventProcessingResult | None:
        batch_id = await self._catalog.find_pending_batch(
            actor_id=context.organization_user_id,
            chat_id=context.chat_id,
        )
        if batch_id is None:
            return None
        view = await self._catalog.get_batch_view(batch_id=batch_id)
        if is_explicit_sku_deferral(context.message_text):
            await self._catalog.defer_batch_skus(
                batch_id=batch_id,
                event_id=context.event_id,
                actor_id=context.organization_user_id,
            )
            outbox_id = await self._outbox.enqueue(
                ProcessingOutcomeDraft(
                    organization_id=context.organization_id,
                    source_event_id=context.event_id,
                    outcome_type=ProcessingOutcomeType.CATALOG_BATCH_CONFIRMATION,
                    aggregate_id=batch_id,
                    chat_id=context.chat_id,
                    payload={},
                )
            )
            return TextEventProcessingResult(
                event_id=context.event_id,
                status=TextEventProcessingStatus.CATALOG_BATCH_CONFIRMATION,
                chat_id=context.chat_id,
                catalog_batch_id=batch_id,
                outbox_id=outbox_id,
            )
        extraction = await self._interpreter.interpret(
            user_text=context.message_text,
            view=view,
        )
        if not extraction.details.applies_to_pending_request:
            return None

        drafts, missing = merge_catalog_batch_details(
            extracted=extraction.details,
            view=view,
        )
        duplicate_lines: dict[str, list[int]] = {}
        line_by_request = {item.request_id: item.line_number for item in view.items}
        for draft in drafts:
            if draft.sku:
                duplicate_lines.setdefault(draft.sku.casefold(), []).append(
                    line_by_request[draft.request_id]
                )
        for sku, lines in duplicate_lines.items():
            if len(lines) > 1:
                missing.append(
                    f"lines {', '.join(str(line) for line in lines)} need different SKUs "
                    f"(all currently use {sku})"
                )

        await self._catalog.save_batch_draft(
            batch_id=batch_id,
            event_id=context.event_id,
            actor_id=context.organization_user_id,
            items=drafts,
        )
        updated = await self._catalog.get_batch_view(batch_id=batch_id)
        if missing or updated.status == "awaiting_details":
            outcome_type = ProcessingOutcomeType.CATALOG_BATCH_DETAILS_REQUIRED
            status = TextEventProcessingStatus.CLARIFICATION_REQUIRED
        else:
            outcome_type = ProcessingOutcomeType.CATALOG_BATCH_CONFIRMATION
            status = TextEventProcessingStatus.CATALOG_BATCH_CONFIRMATION
        outbox_id = await self._outbox.enqueue(
            ProcessingOutcomeDraft(
                organization_id=context.organization_id,
                source_event_id=context.event_id,
                outcome_type=outcome_type,
                aggregate_id=batch_id,
                chat_id=context.chat_id,
                payload={"missing": missing},
            )
        )
        return TextEventProcessingResult(
            event_id=context.event_id,
            status=status,
            chat_id=context.chat_id,
            catalog_batch_id=batch_id,
            outbox_id=outbox_id,
        )


__all__ = ["CatalogBatchDetailsInterpreter", "CatalogBatchReplyHandler"]
