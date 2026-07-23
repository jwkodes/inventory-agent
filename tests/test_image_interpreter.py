"""Vision interpreter tests using a fake Responses API boundary."""

import base64
from types import SimpleNamespace
from typing import Any

import pytest

from inventory_agent.extraction.image_interpreter import (
    IMAGE_PROMPT_VERSION,
    OpenAIImageCommandInterpreter,
)
from inventory_agent.extraction.schema import ExtractedInventoryCommand


class FakeResponses:
    def __init__(self, response: object) -> None:
        self.response = response
        self.arguments: dict[str, Any] = {}

    async def parse(self, **kwargs: Any) -> object:
        self.arguments = kwargs
        return self.response


class FakeOpenAI:
    def __init__(self, response: object) -> None:
        self.responses = FakeResponses(response)


def command() -> ExtractedInventoryCommand:
    return ExtractedInventoryCommand.model_validate(
        {
            "schema_version": "1.0",
            "intent": "RECEIVE_STOCK",
            "location_hint": None,
            "lines": [
                {
                    "source_text": "ABC-123 3 BOX",
                    "item_reference": {"type": "PART_NUMBER", "value": "ABC-123"},
                    "description": "Widget",
                    "quantity": "3",
                    "unit": "box",
                    "attributes": [],
                }
            ],
            "notes": "Invoice INV-7",
            "needs_clarification": False,
            "clarification_question": None,
        }
    )


async def test_image_interpreter_sends_base64_image_and_structured_schema() -> None:
    response = SimpleNamespace(
        output_parsed=command(),
        output=[],
        id="resp-image",
        model="gpt-test",
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120),
    )
    client = FakeOpenAI(response)
    interpreter = OpenAIImageCommandInterpreter(
        client=client,  # type: ignore[arg-type]
        model="gpt-test",
    )

    result = await interpreter.interpret(
        image_bytes=b"fake-jpeg",
        media_type="image/jpeg",
        caption="incoming delivery",
    )

    content = client.responses.arguments["input"][0]["content"]
    assert content[0]["text"] == "Telegram caption: incoming delivery"
    assert content[1]["image_url"] == (
        f"data:image/jpeg;base64,{base64.b64encode(b'fake-jpeg').decode('ascii')}"
    )
    assert content[1]["detail"] == "high"
    assert client.responses.arguments["text_format"] is ExtractedInventoryCommand
    assert client.responses.arguments["store"] is False
    assert result.prompt_version == IMAGE_PROMPT_VERSION
    assert result.total_tokens == 120


async def test_image_interpreter_rejects_unsupported_media_without_api_call() -> None:
    client = FakeOpenAI(SimpleNamespace())
    interpreter = OpenAIImageCommandInterpreter(
        client=client,  # type: ignore[arg-type]
        model="gpt-test",
    )

    with pytest.raises(ValueError, match="Unsupported"):
        await interpreter.interpret(
            image_bytes=b"%PDF",
            media_type="application/pdf",
        )

    assert client.responses.arguments == {}
