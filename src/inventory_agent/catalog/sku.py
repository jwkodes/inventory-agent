"""Recognize an explicit user decision to assign a catalog SKU later."""

import re


def is_explicit_sku_deferral(text: str) -> bool:
    """Return true only for language that clearly postpones or declines an SKU."""

    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    return bool(
        re.search(r"\b(?:no|skip|ignore|without|omit|leave)\b.{0,20}\bsku\b", normalized)
        or re.search(r"\bno need\b.{0,20}\bsku\b", normalized)
        or re.search(r"\bsku\b.{0,20}\b(?:later|now|skip|ignore|omit)\b", normalized)
        or re.search(r"\bdo not have\b.{0,20}\bsku\b", normalized)
        or re.search(r"\bdon t have\b.{0,20}\bsku\b", normalized)
    )


__all__ = ["is_explicit_sku_deferral"]
