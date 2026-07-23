"""Durable Telegram invoice-image processing."""

import hashlib
from pathlib import PurePosixPath
from typing import Protocol

from inventory_agent.artifacts.repository import (
    SourceArtifactDraft,
    SourceArtifactRepository,
)
from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.processing.commands import InventoryCommandHandler
from inventory_agent.processing.models import (
    ImageEventProcessingResult,
    TelegramImageEventContext,
)
from inventory_agent.processing.repository import SourceEventWorkRepository
from inventory_agent.telegram.client import DownloadedTelegramFile


class InvoiceImageInterpreter(Protocol):
    async def interpret(
        self,
        *,
        image_bytes: bytes,
        media_type: str,
        caption: str | None = None,
    ) -> CommandExtractionResult:
        """Extract a strict command from one invoice image."""


class TelegramFileDownloader(Protocol):
    async def download_file(
        self,
        *,
        file_id: str,
        expected_size: int | None = None,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> DownloadedTelegramFile:
        """Download one Telegram file."""


class ImageEventProcessingError(RuntimeError):
    """A claimed image event failed and was recorded for retry."""


class TelegramImageEventProcessor:
    def __init__(
        self,
        *,
        events: SourceEventWorkRepository,
        downloader: TelegramFileDownloader,
        artifacts: SourceArtifactRepository,
        interpreter: InvoiceImageInterpreter,
        commands: InventoryCommandHandler,
    ) -> None:
        self._events = events
        self._downloader = downloader
        self._artifacts = artifacts
        self._interpreter = interpreter
        self._commands = commands

    async def process_next(self) -> ImageEventProcessingResult | None:
        context = await self._events.claim_next_image_event()
        if context is None:
            return None
        return await self._process_claimed(context)

    async def _process_claimed(
        self,
        context: TelegramImageEventContext,
    ) -> ImageEventProcessingResult:
        try:
            downloaded = await self._downloader.download_file(
                file_id=context.telegram_file_id,
                expected_size=context.file_size,
            )
            digest = hashlib.sha256(downloaded.data).hexdigest()
            extension = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
            }[context.media_type]
            storage_path = PurePosixPath(
                str(context.organization_id),
                str(context.event_id),
                f"{digest}{extension}",
            ).as_posix()
            await self._artifacts.store(
                SourceArtifactDraft(
                    organization_id=context.organization_id,
                    source_event_id=context.event_id,
                    storage_path=storage_path,
                    media_type=context.media_type,
                    sha256=digest,
                    telegram_file_id=context.telegram_file_id,
                    data=downloaded.data,
                    metadata={
                        "telegram_file_unique_id": context.telegram_file_unique_id,
                        "original_file_name": context.original_file_name,
                        "telegram_file_path": downloaded.file_path,
                        "file_size": len(downloaded.data),
                        "width": context.width,
                        "height": context.height,
                    },
                )
            )
            extraction = await self._interpreter.interpret(
                image_bytes=downloaded.data,
                media_type=context.media_type,
                caption=context.caption,
            )
            result = await self._commands.handle(context=context, extraction=extraction)
            if not await self._events.finish_event(event_id=context.event_id, success=True):
                raise RuntimeError("Claimed image event could not be completed")
            return result
        except Exception as error:
            failure = f"{type(error).__name__}: image event processing failed"
            try:
                await self._events.finish_event(
                    event_id=context.event_id,
                    success=False,
                    error_message=failure,
                )
            except Exception as finish_error:
                raise ImageEventProcessingError(
                    "Image event processing and failure recording both failed"
                ) from finish_error
            raise ImageEventProcessingError("Image event processing failed") from error
