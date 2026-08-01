"""Tests for explicit SKU deferral language."""

from inventory_agent.catalog.sku import is_explicit_sku_deferral


def test_explicit_sku_deferral_phrases_are_recognized() -> None:
    assert is_explicit_sku_deferral("no SKU for now")
    assert is_explicit_sku_deferral("ignore sku for now")
    assert is_explicit_sku_deferral("I don't have an SKU for this")
    assert is_explicit_sku_deferral("continue without a SKU")
    assert is_explicit_sku_deferral("no need to record SKU")


def test_sku_values_and_unrelated_no_language_are_not_deferrals() -> None:
    assert not is_explicit_sku_deferral("use SKU MILO-500")
    assert not is_explicit_sku_deferral("no, use the existing product")
