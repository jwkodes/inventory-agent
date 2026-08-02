"""Tool-loop tests independent of the live OpenAI API."""

import json
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from inventory_agent.agent.models import CatalogVariant
from inventory_agent.agent.runtime import (
    FunctionCall,
    InventoryAgentSession,
    ModelTurn,
    OpenAIResponsesAgentModel,
    build_prompt_cache_key,
)
from inventory_agent.agent.tools import SimulatedInventoryTools


@dataclass
class FakeModel:
    turns: list[ModelTurn]

    def __post_init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.instructions: list[str] = []
        self.prompt_cache_keys: list[str | None] = []
        self.prompt_cache_prefix_item_counts: list[int | None] = []

    async def respond(
        self,
        *,
        input_items: list[dict[str, object]],
        instructions: str,
        tools: list[dict[str, object]],
        prompt_cache_key: str | None = None,
        prompt_cache_prefix_item_count: int | None = None,
    ) -> ModelTurn:
        self.requests.append(list(input_items))
        self.instructions.append(instructions)
        self.prompt_cache_keys.append(prompt_cache_key)
        self.prompt_cache_prefix_item_counts.append(prompt_cache_prefix_item_count)
        assert "inventory assistant" in instructions
        assert {tool["name"] for tool in tools} >= {
            "read_inventory",
            "propose_add_inventory",
            "propose_reversal",
        }
        return self.turns.pop(0)


def turn(
    number: int,
    *,
    call: FunctionCall | None = None,
    text: str = "",
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> ModelTurn:
    output_items: list[dict[str, object]]
    calls: list[FunctionCall]
    if call is None:
        output_items = [
            {
                "id": f"message-{number}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
        calls = []
    else:
        output_items = [
            {
                "id": f"function-{number}",
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments),
            }
        ]
        calls = [call]
    return ModelTurn(
        response_id=f"response-{number}",
        model="fake-model",
        output_items=output_items,
        output_text=text,
        function_calls=calls,
        input_tokens=10,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=5,
        total_tokens=15,
    )


@pytest.mark.asyncio
async def test_agent_runs_reads_then_proposal_then_user_response() -> None:
    model = FakeModel(
        turns=[
            turn(
                1,
                call=FunctionCall(
                    call_id="call-read",
                    name="read_inventory",
                    arguments={
                        "query": None,
                        "sku": "ABC-123",
                        "attributes": [],
                        "include_zero_stock": True,
                        "limit": 5,
                    },
                ),
            ),
            turn(
                2,
                call=FunctionCall(
                    call_id="call-add",
                    name="propose_add_inventory",
                    arguments={
                        "lines": [
                            {
                                "variant_id": "variant-abc",
                                "new_item": None,
                                "quantity": 3,
                                "unit": "each",
                                "attributes": [],
                            }
                        ],
                        "reason": "Delivery",
                    },
                ),
            ),
            turn(3, text="I prepared a receipt for 3 widgets. Confirmation is required."),
        ]
    )
    tools = SimulatedInventoryTools(
        catalog=[
            CatalogVariant(
                variant_id="variant-abc",
                item_name="Widget",
                sku="ABC-123",
                on_hand=Decimal("2"),
            ),
        ]
    )

    reply = await InventoryAgentSession(model=model, tools=tools).handle("We received 3 of ABC-123")

    assert [trace.name for trace in reply.tool_traces] == [
        "read_inventory",
        "propose_add_inventory",
    ]
    assert reply.total_tokens == 45
    assert len(tools.proposals) == 1
    assert tools.proposals[0].operation == "ADD"
    third_request = model.requests[2]
    assert any(item.get("type") == "function_call_output" for item in third_request)


@pytest.mark.asyncio
async def test_explicit_sku_deferral_creates_proposal_and_returns_short_reply() -> None:
    model = FakeModel(
        turns=[
            turn(
                1,
                call=FunctionCall(
                    call_id="call-add-macbook",
                    name="propose_add_inventory",
                    arguments={
                        "lines": [
                            {
                                "variant_id": None,
                                "new_item": {
                                    "name": "MacBook Air M5",
                                    "sku": None,
                                    "sku_deferred": True,
                                    "base_unit": "each",
                                    "tracking_mode": "simple",
                                    "attributes": [],
                                },
                                "quantity": 10,
                                "unit": "each",
                                "attributes": [],
                            }
                        ],
                        "reason": "Apple delivery",
                    },
                ),
            ),
            turn(2, text="The new product is ready for review without an SKU."),
        ]
    )
    tools = SimulatedInventoryTools(catalog=[])
    session = InventoryAgentSession(model=model, tools=tools)

    reply = await session.handle("No need to record SKU")

    assert reply.text == "The new product is ready for review without an SKU."
    assert len(model.requests) == 2
    assert len(tools.proposals) == 1
    assert tools.proposals[0].payload["lines"][0]["new_item"]["sku"] is None
    assert session.history[-1]["role"] == "assistant"
    assert session.history[-1]["content"][0]["text"] == reply.text


@pytest.mark.asyncio
async def test_agent_preserves_history_across_natural_follow_up() -> None:
    model = FakeModel(
        turns=[
            turn(1, text="Which colour and sizes are they?"),
            turn(2, text="Thanks. I still need the quantity split."),
        ]
    )
    session = InventoryAgentSession(
        model=model,
        tools=SimulatedInventoryTools(catalog=[]),
    )

    await session.handle("I received four shirts")
    await session.handle("They are black, in L and XS")

    second_request = model.requests[1]
    assert second_request[0] == {"role": "user", "content": "I received four shirts"}
    assert second_request[-1] == {
        "role": "user",
        "content": "They are black, in L and XS",
    }


@pytest.mark.asyncio
async def test_agent_supplies_compacted_summary_as_untrusted_instructions() -> None:
    model = FakeModel(turns=[turn(1, text="What would you like to do next?")])
    session = InventoryAgentSession(
        model=model,
        tools=SimulatedInventoryTools(catalog=[]),
        summary="The user previously discussed AMOX-500.",
    )

    await session.handle("What was I working on?")

    assert "untrusted reference data" in model.instructions[0]
    assert "The user previously discussed AMOX-500." in model.instructions[0]
    assert model.requests[0] == [{"role": "user", "content": "What was I working on?"}]


@pytest.mark.asyncio
async def test_cache_enabled_session_separates_summary_from_stable_instructions() -> None:
    model = FakeModel(
        turns=[
            turn(
                1,
                text="What would you like to do next?",
                cached_input_tokens=7,
                cache_write_tokens=3,
            )
        ]
    )
    session = InventoryAgentSession(
        model=model,
        tools=SimulatedInventoryTools(catalog=[]),
        summary="The user previously discussed AMOX-500.",
        prompt_cache_key="a" * 64,
    )

    reply = await session.handle("What was I working on?")

    assert "earlier_conversation_summary" not in model.instructions[0]
    assert model.requests[0][0]["role"] == "developer"
    assert "AMOX-500" in str(model.requests[0][0]["content"])
    assert model.requests[0][-1] == {
        "role": "user",
        "content": "What was I working on?",
    }
    assert model.prompt_cache_keys == ["a" * 64]
    assert model.prompt_cache_prefix_item_counts == [2]
    assert reply.cached_input_tokens == 7
    assert reply.cache_write_tokens == 3


class FakeResponseItem:
    type = "message"

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, object]:
        assert (mode, exclude_none) == ("json", True)
        return {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Done"}],
        }


class RecordingResponses:
    def __init__(self) -> None:
        self.arguments: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> object:
        self.arguments = kwargs
        return SimpleNamespace(
            id="response-cache",
            model=kwargs["model"],
            output=[FakeResponseItem()],
            output_text="Done",
            usage=SimpleNamespace(
                input_tokens=2000,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=1536,
                    cache_write_tokens=256,
                ),
                output_tokens=20,
                total_tokens=2020,
            ),
        )


class RecordingOpenAI:
    def __init__(self) -> None:
        self.responses = RecordingResponses()


@pytest.mark.asyncio
async def test_gpt_5_6_request_uses_explicit_cache_breakpoints_and_reports_usage() -> None:
    client = RecordingOpenAI()
    model = OpenAIResponsesAgentModel(
        client=client,  # type: ignore[arg-type]
        model="gpt-5.6-sol",
    )
    cache_key = build_prompt_cache_key(UUID("65000000-0000-0000-0000-000000000001"))

    result = await model.respond(
        input_items=[{"role": "user", "content": "Show stock"}],
        instructions="Stable instructions",
        tools=[],
        prompt_cache_key=cache_key,
        prompt_cache_prefix_item_count=1,
    )

    arguments = client.responses.arguments
    assert arguments["prompt_cache_key"] == cache_key
    assert arguments["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    request_input = arguments["input"]
    assert request_input[0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert request_input[1]["content"][0]["text"] == "Show stock"
    assert request_input[1]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert arguments["store"] is False
    assert result.cached_input_tokens == 1536
    assert result.cache_write_tokens == 256


@pytest.mark.asyncio
async def test_older_model_uses_cache_key_without_explicit_breakpoints() -> None:
    client = RecordingOpenAI()
    model = OpenAIResponsesAgentModel(
        client=client,  # type: ignore[arg-type]
        model="gpt-5.5",
    )
    input_items = [{"role": "user", "content": "Show stock"}]

    await model.respond(
        input_items=input_items,
        instructions="Stable instructions",
        tools=[],
        prompt_cache_key="b" * 64,
        prompt_cache_prefix_item_count=1,
    )

    arguments = client.responses.arguments
    assert arguments["prompt_cache_key"] == "b" * 64
    assert "prompt_cache_options" not in arguments
    assert arguments["input"] == input_items


def test_prompt_cache_key_is_stable_opaque_and_conversation_scoped() -> None:
    first_id = UUID("65000000-0000-0000-0000-000000000001")
    second_id = UUID("65000000-0000-0000-0000-000000000002")

    first = build_prompt_cache_key(first_id)

    assert first == build_prompt_cache_key(first_id)
    assert first != build_prompt_cache_key(second_id)
    assert len(first) == 64
    assert str(first_id) not in first


@pytest.mark.asyncio
async def test_agent_stops_after_tool_round_budget() -> None:
    model = FakeModel(
        turns=[
            turn(
                1,
                call=FunctionCall(
                    call_id="read-1",
                    name="read_inventory",
                    arguments={
                        "query": None,
                        "sku": None,
                        "attributes": [],
                        "include_zero_stock": True,
                        "limit": 5,
                    },
                ),
            )
        ]
    )
    session = InventoryAgentSession(
        model=model,
        tools=SimulatedInventoryTools(catalog=[]),
        max_tool_rounds=1,
    )

    reply = await session.handle("Show stock")

    assert "tool limit" in reply.text
