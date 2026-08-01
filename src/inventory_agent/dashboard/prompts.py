"""Expose the current LLM contracts to the development dashboard."""

from inventory_agent.agent.context import SUMMARY_INSTRUCTIONS, SUMMARY_PROMPT_VERSION
from inventory_agent.agent.prompt import (
    INSTRUCTIONS as AGENT_INSTRUCTIONS,
)
from inventory_agent.agent.prompt import (
    PROMPT_VERSION as AGENT_PROMPT_VERSION,
)
from inventory_agent.agent.tools import INVENTORY_TOOL_DEFINITIONS
from inventory_agent.catalog.interpreter import (
    CATALOG_DETAILS_INSTRUCTIONS,
    CATALOG_DETAILS_PROMPT_VERSION,
)
from inventory_agent.extraction.image_interpreter import (
    IMAGE_PROMPT_VERSION,
    INVOICE_INSTRUCTIONS,
)
from inventory_agent.extraction.interpreter import (
    INSTRUCTIONS as EXTRACTION_INSTRUCTIONS,
)
from inventory_agent.extraction.interpreter import (
    PROMPT_VERSION as EXTRACTION_PROMPT_VERSION,
)
from inventory_agent.matching.judge import (
    INSTRUCTIONS as JUDGE_INSTRUCTIONS,
)
from inventory_agent.matching.judge import (
    PROMPT_VERSION as JUDGE_PROMPT_VERSION,
)


def prompt_catalog(
    *,
    agent_model: str,
    agent_reasoning_effort: str,
    extraction_model: str,
    extraction_reasoning_effort: str,
    embedding_model: str,
    agent_enabled: bool,
    context_policy: str,
    candidate_judging_enabled: bool,
    matching_strategy: str,
) -> list[dict[str, object]]:
    """Return current prompts and model configuration without any credentials."""

    return [
        {
            "layer": "inventory_agent",
            "label": "Inventory agent",
            "prompt_version": AGENT_PROMPT_VERSION,
            "model": agent_model,
            "reasoning_effort": agent_reasoning_effort,
            "runtime_status": "active" if agent_enabled else "standby",
            "when_called": (
                "Ordinary Telegram text after pending reversal and catalog-detail "
                "flows have been checked."
            ),
            "action": (
                "Converses with the user and calls grounded inventory, proposal, "
                "transaction and reversal tools."
            ),
            "instructions": AGENT_INSTRUCTIONS,
            "tools": INVENTORY_TOOL_DEFINITIONS,
        },
        {
            "layer": "command_extraction",
            "label": "Structured text extraction",
            "prompt_version": EXTRACTION_PROMPT_VERSION,
            "model": extraction_model,
            "reasoning_effort": extraction_reasoning_effort,
            "runtime_status": "standby" if agent_enabled else "active",
            "when_called": ("Ordinary Telegram text only when INVENTORY_AGENT_ENABLED=false."),
            "action": "Converts free text into a validated structured inventory command.",
            "instructions": EXTRACTION_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "context_summary",
            "label": "Conversation context summary",
            "prompt_version": SUMMARY_PROMPT_VERSION,
            "model": agent_model,
            "reasoning_effort": agent_reasoning_effort,
            "runtime_status": (
                "conditional" if agent_enabled and context_policy == "summarize" else "disabled"
            ),
            "when_called": (
                "Only when conversation age, estimated tokens, or item count exceeds "
                "the configured context limit."
            ),
            "action": "Replaces compacted conversational history with a rolling summary.",
            "instructions": SUMMARY_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "invoice_extraction",
            "label": "Invoice image extraction",
            "prompt_version": IMAGE_PROMPT_VERSION,
            "model": extraction_model,
            "reasoning_effort": extraction_reasoning_effort,
            "runtime_status": "conditional",
            "when_called": "Every supported Telegram invoice image.",
            "action": "Reads the stored image and emits validated structured invoice lines.",
            "instructions": INVOICE_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "catalog_details",
            "label": "Catalog detail extraction",
            "prompt_version": CATALOG_DETAILS_PROMPT_VERSION,
            "model": extraction_model,
            "reasoning_effort": extraction_reasoning_effort,
            "runtime_status": "conditional",
            "when_called": (
                "A natural-language reply while a new catalog item is awaiting details."
            ),
            "action": "Extracts only the missing item name, SKU, unit, or attributes.",
            "instructions": CATALOG_DETAILS_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "candidate_judge",
            "label": "Candidate judge",
            "prompt_version": JUDGE_PROMPT_VERSION,
            "model": extraction_model,
            "reasoning_effort": extraction_reasoning_effort,
            "runtime_status": ("conditional" if candidate_judging_enabled else "disabled"),
            "when_called": (
                "The invoice or legacy structured pipeline has retrieved candidates "
                "that require a constrained match decision."
            ),
            "action": "Selects a retrieved candidate, asks one question, or rejects all.",
            "instructions": JUDGE_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "semantic_retrieval",
            "label": "Semantic retrieval",
            "prompt_version": None,
            "model": embedding_model,
            "reasoning_effort": "not applicable",
            "runtime_status": ("conditional" if matching_strategy != "fuzzy" else "disabled"),
            "when_called": (
                "A name-based catalog search uses semantic or hybrid matching; exact "
                "SKU reads and broad listings bypass it."
            ),
            "action": "Embeds the search phrase and retrieves catalog candidates by similarity.",
            "instructions": (
                "Embeds normalized item names, variant names, SKUs, aliases, and searchable "
                "attributes. Cosine similarity retrieves candidates; deterministic policy "
                "and optional candidate judgment decide the next action."
            ),
            "tools": [],
        },
    ]
