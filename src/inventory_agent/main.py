"""FastAPI application entry point."""

import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import FastAPI, Request
from starlette.responses import Response

from inventory_agent import __version__
from inventory_agent.dashboard.router import router as dashboard_router
from inventory_agent.telegram.router import router as telegram_router

logger = logging.getLogger(__name__)


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

    @application.middleware("http")
    async def log_webhook_runtime(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = perf_counter()
        response = await call_next(request)
        if request.url.path == "/webhooks/telegram":
            logger.info(
                "component_runtime component=telegram_webhook_ingest duration_ms=%.2f "
                "status_code=%s",
                (perf_counter() - started) * 1000,
                response.status_code,
            )
        return response

    application.include_router(dashboard_router)
    application.include_router(telegram_router)
    application.add_api_route("/health", health, methods=["GET"], tags=["operations"])
    return application


app = create_app()
