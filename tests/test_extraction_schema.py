"""Validation tests for the versioned inventory command contract."""

import pytest
from pydantic import ValidationError

from inventory_agent.extraction.schema import ExtractedInventoryCommand


def receive_command() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "intent": "RECEIVE_STOCK",
        "location_hint": None,
        "lines": [
            {
                "source_text": "3 red shirts, part SHIRT-RED-M",
                "item_reference": {"type": "PART_NUMBER", "value": "SHIRT-RED-M"},
                "description": "red shirts",
                "quantity": "3",
                "unit": "each",
                "attributes": [{"key": "colour", "value": "red"}],
            }
        ],
        "notes": None,
        "needs_clarification": False,
        "clarification_question": None,
    }


def test_valid_command_preserves_decimal_string_and_custom_attributes() -> None:
    command = ExtractedInventoryCommand.model_validate(receive_command())

    assert command.lines[0].quantity == "3"
    assert command.lines[0].attributes[0].key == "colour"


def test_mutation_without_quantity_normalizes_to_clarification() -> None:
    payload = receive_command()
    payload["lines"][0]["quantity"] = None  # type: ignore[index]

    command = ExtractedInventoryCommand.model_validate(payload)

    assert command.needs_clarification is True
    assert command.clarification_question == "What quantity should I use for each item?"


def test_quantity_must_be_positive_finite_decimal_string() -> None:
    payload = receive_command()
    payload["lines"][0]["quantity"] = "0"  # type: ignore[index]

    with pytest.raises(ValidationError, match="greater than zero"):
        ExtractedInventoryCommand.model_validate(payload)


def test_unknown_intent_normalizes_to_clarification() -> None:
    payload = receive_command()
    payload.update(intent="UNKNOWN", lines=[], needs_clarification=False)

    command = ExtractedInventoryCommand.model_validate(payload)

    assert command.needs_clarification is True
    assert command.clarification_question == "What inventory change would you like to make?"


def test_question_normalizes_clarification_flag() -> None:
    payload = receive_command()
    payload["clarification_question"] = "Which location?"

    command = ExtractedInventoryCommand.model_validate(payload)

    assert command.needs_clarification is True
    assert command.clarification_question == "Which location?"


def test_arbitrary_extra_fields_are_rejected() -> None:
    payload = receive_command()
    payload["database_item_id"] = "invented-id"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExtractedInventoryCommand.model_validate(payload)
