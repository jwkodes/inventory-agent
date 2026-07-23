"""Parse deterministic Telegram catalog-detail forms without an LLM call."""

import json

from pydantic import ValidationError

from inventory_agent.catalog.models import (
    CatalogItemCreationView,
    CatalogItemDetails,
    CatalogTrackingMode,
    ExtractedCatalogItemDetails,
)

DETAILS_EXAMPLE = (
    "Name: <item name>\n"
    "SKU: <SKU or internal code>\n"
    "Base unit: each\n"
    "Tracking: simple\n"
    "Attributes: {}"
)


def parse_catalog_item_details(text: str) -> CatalogItemDetails:
    """Parse one key/value-per-line form and validate its required fields."""

    values: dict[str, str] = {}
    aliases = {
        "name": "name",
        "sku": "sku",
        "part number": "sku",
        "base unit": "base_unit",
        "unit": "base_unit",
        "tracking": "tracking_mode",
        "tracking mode": "tracking_mode",
        "attributes": "attributes",
    }
    for raw_line in text.splitlines():
        key, separator, value = raw_line.partition(":")
        normalized_key = aliases.get(key.strip().lower())
        if not separator or normalized_key is None or not value.strip():
            continue
        values[normalized_key] = value.strip()

    attributes: object = {}
    if "attributes" in values:
        try:
            attributes = json.loads(values["attributes"])
        except json.JSONDecodeError as error:
            raise ValueError("Attributes must be a valid JSON object") from error
        if not isinstance(attributes, dict):
            raise ValueError("Attributes must be a JSON object")

    try:
        return CatalogItemDetails.model_validate(
            {
                "name": values.get("name"),
                "sku": values.get("sku"),
                "base_unit": values.get("base_unit"),
                "tracking_mode": values.get("tracking_mode"),
                "attributes": attributes,
            }
        )
    except ValidationError as error:
        raise ValueError("Name, SKU, base unit, and tracking are required") from error


def complete_catalog_item_details(
    *,
    extracted: ExtractedCatalogItemDetails,
    view: CatalogItemCreationView,
) -> tuple[CatalogItemDetails | None, list[str]]:
    """Merge safe request suggestions and report information still required."""

    name = _clean(extracted.name) or _clean(view.name) or _clean(view.suggested_name)
    sku = _clean(extracted.sku) or _clean(view.sku) or _clean(view.suggested_sku)
    base_unit = (
        _clean(extracted.base_unit) or _clean(view.base_unit) or _clean(view.suggested_base_unit)
    )
    tracking_mode = extracted.tracking_mode or view.tracking_mode or view.suggested_tracking_mode
    attributes = {
        **view.attributes,
        **{
            attribute.key.strip(): attribute.value.strip()
            for attribute in extracted.attributes
            if attribute.key.strip() and attribute.value.strip()
        },
    }

    missing: list[str] = []
    if name is None:
        missing.append("item name")
    if sku is None:
        missing.append("SKU or internal product code")
    if base_unit is None:
        missing.append("base unit")
    if tracking_mode is not CatalogTrackingMode.SIMPLE:
        missing.append("simple tracking (lot and serial tracking are not supported yet)")
    if missing:
        return None, missing
    assert name is not None
    assert sku is not None
    assert base_unit is not None

    return (
        CatalogItemDetails(
            name=name,
            sku=sku,
            base_unit=base_unit,
            tracking_mode=tracking_mode,
            attributes=attributes,
        ),
        [],
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
