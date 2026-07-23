"""Small, forward-compatible models for the Telegram update fields we consume."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TelegramUser(BaseModel):
    """Telegram user identity."""

    model_config = ConfigDict(extra="allow")

    id: int


class TelegramMessage(BaseModel):
    """Message fields needed to determine the acting Telegram user."""

    model_config = ConfigDict(extra="allow")

    sender: TelegramUser | None = Field(default=None, alias="from")


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
