"""Tests for deterministic catalog detail-form parsing."""

import pytest

from inventory_agent.catalog.details import parse_catalog_item_details
from inventory_agent.catalog.models import CatalogTrackingMode


def test_catalog_detail_form_parses_required_fields_and_attributes() -> None:
    details = parse_catalog_item_details(
        "Name: Purple Widget\n"
        "SKU: ZX-999\n"
        "Base unit: each\n"
        "Tracking: simple\n"
        'Attributes: {"colour":"purple"}'
    )

    assert details.name == "Purple Widget"
    assert details.sku == "ZX-999"
    assert details.base_unit == "each"
    assert details.tracking_mode is CatalogTrackingMode.SIMPLE
    assert details.attributes == {"colour": "purple"}


@pytest.mark.parametrize(
    "text",
    [
        "Name: Purple Widget\nSKU: ZX-999",
        "Name: Purple Widget\nSKU: ZX-999\nBase unit: each\nTracking: invalid",
        (
            "Name: Purple Widget\nSKU: ZX-999\nBase unit: each\nTracking: simple\n"
            "Attributes: not-json"
        ),
    ],
)
def test_catalog_detail_form_rejects_incomplete_or_invalid_input(text: str) -> None:
    with pytest.raises(ValueError):
        parse_catalog_item_details(text)
