"""Safety and contract tests for the no-write inventory tool spike."""

import json
from decimal import Decimal

import pytest

from inventory_agent.agent.models import (
    CatalogVariant,
    NewCatalogItemDraft,
    TrackingMode,
    TransactionRecord,
)
from inventory_agent.agent.tools import INVENTORY_TOOL_DEFINITIONS, SimulatedInventoryTools


def catalog() -> list[CatalogVariant]:
    return [
        CatalogVariant(
            variant_id="variant-1",
            item_name="Classic T-Shirt",
            variant_name="Black / L",
            sku="TS-BLK-L",
            on_hand=Decimal("5"),
        )
    ]


def test_new_catalog_item_tool_schema_allows_simple_tracking_only() -> None:
    add_tool = next(
        tool for tool in INVENTORY_TOOL_DEFINITIONS if tool["name"] == "propose_add_inventory"
    )
    tracking_schema = add_tool["parameters"]["properties"]["lines"]["items"]["properties"][
        "new_item"
    ]["anyOf"][0]["properties"]["tracking_mode"]

    assert tracking_schema == {"type": "string", "enum": ["simple"]}


@pytest.mark.parametrize("base_unit", ["each", "unit", "units", "item", "items", " UNIT "])
def test_new_catalog_item_normalizes_generic_count_words_to_each(base_unit: str) -> None:
    item = NewCatalogItemDraft(
        name="Nintendo Switch second edition",
        sku="SWITCH-2",
        base_unit=base_unit,
        tracking_mode=TrackingMode.SIMPLE,
        attributes=[],
    )

    assert item.base_unit == "each"


def test_new_catalog_item_schema_tells_model_not_to_clarify_generic_count_words() -> None:
    add_tool = next(
        tool for tool in INVENTORY_TOOL_DEFINITIONS if tool["name"] == "propose_add_inventory"
    )
    base_unit_schema = add_tool["parameters"]["properties"]["lines"]["items"]["properties"][
        "new_item"
    ]["anyOf"][0]["properties"]["base_unit"]

    assert "Silently use 'each'" in base_unit_schema["description"]
    assert "unit, units, item, and items mean the same thing" in base_unit_schema["description"]


def test_new_catalog_item_schema_states_current_sku_requirement() -> None:
    add_tool = next(
        tool for tool in INVENTORY_TOOL_DEFINITIONS if tool["name"] == "propose_add_inventory"
    )
    new_item_schema = add_tool["parameters"]["properties"]["lines"]["items"]["properties"][
        "new_item"
    ]["anyOf"][0]

    assert "mandatory" in new_item_schema["properties"]["sku"]["description"]
    assert "SKU/internal code is currently mandatory" in add_tool["description"]


@pytest.mark.asyncio
async def test_read_makes_returned_variant_available_to_proposal() -> None:
    tools = SimulatedInventoryTools(catalog=catalog())

    read = json.loads(
        await tools.execute(
            call_id="read-1",
            name="read_inventory",
            arguments={
                "query": "classic shirt",
                "sku": None,
                "attributes": [],
                "include_zero_stock": True,
                "limit": 5,
            },
        )
    )
    proposal = json.loads(
        await tools.execute(
            call_id="add-1",
            name="propose_add_inventory",
            arguments={
                "lines": [
                    {
                        "variant_id": "variant-1",
                        "new_item": None,
                        "quantity": 2,
                        "unit": "each",
                        "attributes": [],
                    }
                ],
                "reason": "Delivery",
            },
        )
    )

    assert read["items"][0]["variant_id"] == "variant-1"
    assert proposal["ok"] is True
    assert proposal["inventory_changed"] is False
    assert tools.proposals[0].status == "awaiting_confirmation"
    assert catalog()[0].on_hand == Decimal("5")


@pytest.mark.asyncio
async def test_new_item_without_sku_requests_input_without_saving_proposal() -> None:
    tools = SimulatedInventoryTools(catalog=[])

    result = json.loads(
        await tools.execute(
            call_id="add-macbook",
            name="propose_add_inventory",
            arguments={
                "lines": [
                    {
                        "variant_id": None,
                        "new_item": {
                            "name": "MacBook Air M5",
                            "sku": None,
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
        )
    )

    assert result["ok"] is False
    assert result["error_code"] == "new_item_sku_required"
    assert result["requires_user_input"] is True
    assert result["user_message"] == (
        "I can't create MacBook Air M5 without an SKU or internal product code yet. "
        "What code should I use?"
    )
    assert tools.proposals == []


@pytest.mark.asyncio
async def test_proposal_rejects_hallucinated_variant_id() -> None:
    tools = SimulatedInventoryTools(catalog=catalog())

    result = json.loads(
        await tools.execute(
            call_id="add-1",
            name="propose_add_inventory",
            arguments={
                "lines": [
                    {
                        "variant_id": "invented-id",
                        "new_item": None,
                        "quantity": 2,
                        "unit": "each",
                        "attributes": [],
                    }
                ],
                "reason": "Delivery",
            },
        )
    )

    assert result["ok"] is False
    assert "not returned by read_inventory" in result["error"]
    assert tools.proposals == []


@pytest.mark.asyncio
async def test_deduction_cannot_create_a_catalog_item() -> None:
    tools = SimulatedInventoryTools(catalog=catalog())

    result = json.loads(
        await tools.execute(
            call_id="deduct-1",
            name="propose_deduct_inventory",
            arguments={
                "lines": [
                    {
                        "variant_id": None,
                        "new_item": {
                            "name": "Unknown item",
                            "sku": None,
                            "base_unit": "each",
                            "tracking_mode": "simple",
                            "attributes": [],
                        },
                        "quantity": 1,
                        "unit": "each",
                        "attributes": [],
                    }
                ],
                "reason": "Usage",
            },
        )
    )

    assert result["ok"] is False
    assert "deductions cannot create" in result["error"]
    assert tools.proposals == []


@pytest.mark.asyncio
async def test_addition_rejects_non_simple_new_catalog_item() -> None:
    tools = SimulatedInventoryTools(catalog=catalog())

    result = json.loads(
        await tools.execute(
            call_id="add-lot-item",
            name="propose_add_inventory",
            arguments={
                "lines": [
                    {
                        "variant_id": None,
                        "new_item": {
                            "name": "Lot item",
                            "sku": None,
                            "base_unit": "each",
                            "tracking_mode": "lot",
                            "attributes": [],
                        },
                        "quantity": 1,
                        "unit": "each",
                        "attributes": [],
                    }
                ],
                "reason": "Delivery",
            },
        )
    )

    assert result["ok"] is False
    assert "simple tracking only" in result["error"]


@pytest.mark.asyncio
async def test_reversal_requires_transaction_from_prior_read() -> None:
    tools = SimulatedInventoryTools(
        catalog=catalog(),
        transactions=[
            TransactionRecord(
                transaction_id="txn-1",
                transaction_type="receive",
                status="applied",
                occurred_at="2026-07-23T09:00:00+08:00",
                summary="Received shirts",
            )
        ],
    )

    rejected = json.loads(
        await tools.execute(
            call_id="reverse-1",
            name="propose_reversal",
            arguments={
                "transaction_ref": "T1",
                "reason": "Wrong count",
                "replacement": None,
            },
        )
    )
    transaction_read = json.loads(
        await tools.execute(
            call_id="transactions-1",
            name="read_transactions",
            arguments={"query": "shirts", "limit": 5},
        )
    )
    accepted = json.loads(
        await tools.execute(
            call_id="reverse-2",
            name="propose_reversal",
            arguments={
                "transaction_ref": "T1",
                "reason": "Wrong count",
                "replacement": None,
            },
        )
    )

    assert rejected["ok"] is False
    assert transaction_read["transactions"][0]["transaction_ref"] == "T1"
    assert accepted["ok"] is True
    assert accepted["inventory_changed"] is False


@pytest.mark.asyncio
async def test_repeated_call_id_is_idempotent() -> None:
    tools = SimulatedInventoryTools(catalog=catalog())
    arguments = {
        "query": None,
        "sku": None,
        "attributes": [],
        "include_zero_stock": True,
        "limit": 5,
    }

    first = await tools.execute(
        call_id="same-call",
        name="read_inventory",
        arguments=arguments,
    )
    second = await tools.execute(
        call_id="same-call",
        name="unknown_tool",
        arguments={},
    )

    assert second == first


@pytest.mark.asyncio
async def test_targeted_read_does_not_pad_results_with_unrelated_items() -> None:
    tools = SimulatedInventoryTools(
        catalog=[
            *catalog(),
            CatalogVariant(
                variant_id="variant-controller",
                item_name="Nintendo Switch 2 Controller",
                sku="NS2-CONTROLLER",
                on_hand=Decimal("3"),
            ),
            CatalogVariant(
                variant_id="variant-widget",
                item_name="Industrial Widget",
                sku="WIDGET",
                on_hand=Decimal("7"),
            ),
        ]
    )

    result = json.loads(
        await tools.execute(
            call_id="read-controller",
            name="read_inventory",
            arguments={
                "query": "Nintendo Switch controller",
                "sku": None,
                "attributes": [],
                "include_zero_stock": True,
                "limit": 20,
            },
        )
    )

    assert [item["variant_id"] for item in result["items"]] == ["variant-controller"]
