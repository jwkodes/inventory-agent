"""Structured Output tests for conversational catalog-detail extraction."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from inventory_agent.catalog.interpreter import OpenAICatalogDetailsInterpreter
from inventory_agent.catalog.models import (
    CatalogItemCreationView,
    ExtractedCatalogItemDetails,
)

REQUEST_ID = UUID("71000000-0000-0000-0000-000000000001")


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


async def test_catalog_interpreter_accepts_natural_language_and_context() -> None:
    parsed = ExtractedCatalogItemDetails(
        applies_to_pending_request=True,
        name="Switch 2 controller",
        sku="SW2-CONTROLLER",
        base_unit="each",
        tracking_mode=None,
        attributes=[],
    )
    response = SimpleNamespace(
        output_parsed=parsed,
        output=[],
        id="resp_catalog",
        model="gpt-test",
    )
    client = FakeOpenAI(response)
    interpreter = OpenAICatalogDetailsInterpreter(
        client=client,  # type: ignore[arg-type]
        model="gpt-test",
    )
    view = CatalogItemCreationView(
        request_id=REQUEST_ID,
        status="awaiting_details",
        suggested_name="switch2 controller",
        suggested_sku=None,
        suggested_base_unit="each",
        suggested_tracking_mode="simple",
    )

    result = await interpreter.interpret(
        user_text="Call it Switch 2 controller and use SW2-CONTROLLER as its code.",
        view=view,
    )

    assert result.details == parsed
    assert client.responses.arguments["text_format"] is ExtractedCatalogItemDetails
    assert client.responses.arguments["store"] is False
    assert "switch2 controller" in client.responses.arguments["input"]
