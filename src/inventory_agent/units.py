"""Canonical inventory-unit helpers shared by agent and catalog boundaries."""

GENERIC_COUNT_UNITS = frozenset({"each", "unit", "units", "item", "items"})


def canonicalize_base_unit(value: str) -> str:
    """Normalize generic one-item vocabulary to the catalog's canonical `each`."""

    cleaned = value.strip()
    if not cleaned:
        raise ValueError("base unit must not be empty")
    if cleaned.casefold() in GENERIC_COUNT_UNITS:
        return "each"
    return cleaned
