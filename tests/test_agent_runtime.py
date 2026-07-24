"""Tool-loop tests independent of the live OpenAI API."""

import json
from dataclasses import dataclass
from decimal import Decimal

import pytest

from inventory_agent.agent.models import CatalogVariant
from inventory_agent.agent.runtime import (
    FunctionCall,
    InventoryAgentSession,
    ModelTurn,
)
from inventory_agent.agent.tools import SimulatedInventoryTools


@dataclass
class FakeModel:
    turns: list[ModelTurn]

    def __post_init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.instructions: list[str] = []

    async def respond(
        self,
        *,
        input_items: list[dict[str, object]],
        instructions: str,
        tools: list[dict[str, object]],
    ) -> ModelTurn:
        self.requests.append(list(input_items))
        self.instructions.append(instructions)
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
            )
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
