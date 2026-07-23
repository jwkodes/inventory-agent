"""FastAPI application entry point."""

from fastapi import FastAPI

from inventory_agent import __version__
from inventory_agent.telegram.router import router as telegram_router


async def health() -> dict[str, str]:
    """Report process health without requiring external service credentials."""

    return {"status": "ok", "service": "inventory-agent"}


def create_app() -> FastAPI:
    """Build an isolated application instance for production and tests."""

    application = FastAPI(
        title="Inventory Agent API",
        summary="Telegram-first inventory transaction service",
        version=__version__,
    )
    application.include_router(telegram_router)
    application.add_api_route("/health", health, methods=["GET"], tags=["operations"])
    return application


app = create_app()
