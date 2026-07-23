"""OpenAI interpreter tests using a local fake response client."""

from types import SimpleNamespace
from typing import Any

import pytest

from inventory_agent.extraction.interpreter import (
    CommandExtractionRefused,
    OpenAITextCommandInterpreter,
)
from inventory_agent.extraction.schema import ExtractedInventoryCommand


def parsed_command() -> ExtractedInventoryCommand:
    return ExtractedInventoryCommand.model_validate(
        {
            "schema_version": "1.0",
            "intent": "RECEIVE_STOCK",
            "location_hint": None,
            "lines": [
                {
                    "source_text": "part ABC-123 and there are 3",
                    "item_reference": {"type": "PART_NUMBER", "value": "ABC-123"},
                    "description": None,
                    "quantity": "3",
                    "unit": None,
                    "attributes": [],
                }
            ],
            "notes": None,
            "needs_clarification": False,
            "clarification_question": None,
        }
    )


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


async def test_interpreter_uses_native_schema_and_returns_audit_metadata() -> None:
    command = parsed_command()
    response = SimpleNamespace(
        output_parsed=command,
        output=[],
        id="resp_test",
        model="gpt-5.6-luna",
        usage=SimpleNamespace(input_tokens=50, output_tokens=25, total_tokens=75),
    )
    client = FakeOpenAI(response)
    interpreter = OpenAITextCommandInterpreter(
        client=client,  # type: ignore[arg-type]
        model="gpt-5.6-luna",
        reasoning_effort="none",
    )

    result = await interpreter.interpret("part ABC-123 and there are 3")

    assert result.command == command
    assert result.response_id == "resp_test"
    assert result.total_tokens == 75
    assert client.responses.arguments["text_format"] is ExtractedInventoryCommand
    assert client.responses.arguments["store"] is False
    assert client.responses.arguments["reasoning"] == {"effort": "none"}


async def test_interpreter_surfaces_model_refusal() -> None:
    refusal_content = SimpleNamespace(type="refusal", refusal="Unable to process this input")
    response = SimpleNamespace(
        output_parsed=None,
        output=[SimpleNamespace(type="message", content=[refusal_content])],
        id="resp_refusal",
        model="gpt-5.6-luna",
        usage=None,
    )
    interpreter = OpenAITextCommandInterpreter(
        client=FakeOpenAI(response),  # type: ignore[arg-type]
        model="gpt-5.6-luna",
    )

    with pytest.raises(CommandExtractionRefused, match="Unable to process"):
        await interpreter.interpret("unsafe input")


async def test_interpreter_rejects_empty_input_without_calling_api() -> None:
    client = FakeOpenAI(SimpleNamespace())
    interpreter = OpenAITextCommandInterpreter(
        client=client,  # type: ignore[arg-type]
        model="gpt-5.6-luna",
    )

    with pytest.raises(ValueError, match="must not be empty"):
        await interpreter.interpret("  ")

    assert client.responses.arguments == {}
