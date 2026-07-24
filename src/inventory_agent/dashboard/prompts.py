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
    extraction_model: str,
    embedding_model: str,
) -> list[dict[str, object]]:
    """Return current prompts and model configuration without any credentials."""

    return [
        {
            "layer": "inventory_agent",
            "label": "Inventory agent",
            "prompt_version": AGENT_PROMPT_VERSION,
            "model": agent_model,
            "instructions": AGENT_INSTRUCTIONS,
            "tools": INVENTORY_TOOL_DEFINITIONS,
        },
        {
            "layer": "command_extraction",
            "label": "Structured text extraction",
            "prompt_version": EXTRACTION_PROMPT_VERSION,
            "model": extraction_model,
            "instructions": EXTRACTION_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "context_summary",
            "label": "Conversation context summary",
            "prompt_version": SUMMARY_PROMPT_VERSION,
            "model": agent_model,
            "instructions": SUMMARY_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "invoice_extraction",
            "label": "Invoice image extraction",
            "prompt_version": IMAGE_PROMPT_VERSION,
            "model": extraction_model,
            "instructions": INVOICE_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "catalog_details",
            "label": "Catalog detail extraction",
            "prompt_version": CATALOG_DETAILS_PROMPT_VERSION,
            "model": extraction_model,
            "instructions": CATALOG_DETAILS_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "candidate_judge",
            "label": "Candidate judge",
            "prompt_version": JUDGE_PROMPT_VERSION,
            "model": extraction_model,
            "instructions": JUDGE_INSTRUCTIONS,
            "tools": [],
        },
        {
            "layer": "semantic_retrieval",
            "label": "Semantic retrieval",
            "prompt_version": None,
            "model": embedding_model,
            "instructions": (
                "Embeds normalized item names, variant names, SKUs, aliases, and searchable "
                "attributes. Cosine similarity retrieves candidates; deterministic policy "
                "and optional candidate judgment decide the next action."
            ),
            "tools": [],
        },
    ]
