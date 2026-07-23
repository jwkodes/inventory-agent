"""Small, forward-compatible models for the Telegram update fields we consume."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SUPPORTED_INVOICE_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class TelegramUser(BaseModel):
    """Telegram user identity."""

    model_config = ConfigDict(extra="allow")

    id: int


class TelegramMessage(BaseModel):
    """Message fields needed to determine the acting Telegram user."""

    model_config = ConfigDict(extra="allow")

    sender: TelegramUser | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None
    photo: list["TelegramPhotoSize"] = Field(default_factory=list)
    document: "TelegramDocument | None" = None

    @property
    def is_supported_invoice_image(self) -> bool:
        return bool(self.photo) or (
            self.document is not None and self.document.mime_type in SUPPORTED_INVOICE_IMAGE_TYPES
        )


class TelegramPhotoSize(BaseModel):
    """One Telegram-generated size of an uploaded photo."""

    model_config = ConfigDict(extra="allow")

    file_id: str
    file_unique_id: str | None = None
    width: int
    height: int
    file_size: int | None = None


class TelegramDocument(BaseModel):
    """Document metadata retained because getFile does not preserve it."""

    model_config = ConfigDict(extra="allow")

    file_id: str
    file_unique_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


class TelegramCallbackQuery(BaseModel):
    """Callback query fields needed for confirmation actions."""

    model_config = ConfigDict(extra="allow")

    sender: TelegramUser = Field(alias="from")


class TelegramUpdate(BaseModel):
    """Subset of an Update used at the trusted webhook boundary.

    Extra fields remain permitted so Telegram can add fields without breaking ingestion.
    The original payload is stored separately for audit and later processing.
    """

    model_config = ConfigDict(extra="allow")

    update_id: int
    message: TelegramMessage | None = None
    edited_message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None

    @property
    def event_type(self) -> str | None:
        """Return the supported top-level Telegram update type."""

        if self.message is not None:
            if self.message.is_supported_invoice_image:
                return "invoice_image"
            if self.message.document is not None:
                return "unsupported_document"
            return "message"
        if self.edited_message is not None:
            return "edited_message"
        if self.callback_query is not None:
            return "callback_query"
        return None

    @property
    def telegram_user_id(self) -> int | None:
        """Return the actor for supported user-originated updates."""

        if self.message is not None and self.message.sender is not None:
            return self.message.sender.id
        if self.edited_message is not None and self.edited_message.sender is not None:
            return self.edited_message.sender.id
        if self.callback_query is not None:
            return self.callback_query.sender.id
        return None


TelegramPayload = dict[str, Any]
