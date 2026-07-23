"""Experimental LLM-led inventory agent.

The package is intentionally disconnected from the production Telegram worker and
Supabase mutation functions while the architecture is being evaluated.
"""

from inventory_agent.agent.runtime import InventoryAgentSession

__all__ = ["InventoryAgentSession"]
