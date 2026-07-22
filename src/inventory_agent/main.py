"""FastAPI application entry point."""

from fastapi import FastAPI

from inventory_agent import __version__

app = FastAPI(
    title="Inventory Agent API",
    summary="Telegram-first inventory transaction service",
    version=__version__,
)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    """Report process health without requiring external service credentials."""

    return {"status": "ok", "service": "inventory-agent"}
