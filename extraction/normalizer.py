"""
Normalization Utilities.
Transforms extracted strings into clean, typed canonical representations
(Decimals, Dates, Standardized UOMs, Currencies) using centralized registries.
Ensures numeric tokens are never glued across whitespace or adjacent columns.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Optional
from dateutil import parser as date_parser

from extraction.semantic_registry import CURRENCY_MAP, UOM_MAP


def normalize_decimal(val: Optional[Any], default: Optional[Decimal] = None) -> Optional[Decimal]:
    """
    Safely extracts and parses a standalone numeric token to Decimal.
    Strictly rejects alphanumeric identifiers, model codes, and dimension expressions (e.g. M40, 6205-2RS, DN50, 4SQMM).
    Supports standalone prices with currency prefixes, commas, and trailing UOM slashes.
    """
    if val is None:
        return default
    if isinstance(val, (int, float, Decimal)):
        return Decimal(str(val))
    val_str = str(val).strip()
    if not val_str:
        return default

    # Known non-numeric placeholders
    if val_str.lower() in ["none", "null", "n/a", "na", "-", "—", "nil", ""]:
        return default

    # Remove currency symbols and surrounding formatting
    cleaned = re.sub(r"[₹$€£¥]", "", val_str).strip()

    # Strip common currency words at start/end
    cleaned = re.sub(r"^(?:inr|usd|eur|gbp|rs\.?|aud|cad|sgd|aed)\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s*(?:inr|usd|eur|gbp|rs\.?|aud|cad|sgd|aed)$", "", cleaned, flags=re.IGNORECASE).strip()

    # Remove formatting commas between digits
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", cleaned)

    # 1. Clean standalone number (e.g. '180', '180.50', '-12.5', '+100')
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cleaned):
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return default

    # 2. Number with UOM suffix attached with slash or space (e.g. '180/pcs', '180 / unit', '180.50/MTR')
    slash_match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*(?:/|per)\s*[a-zA-Z]+", cleaned, flags=re.IGNORECASE)
    if slash_match:
        try:
            return Decimal(slash_match.group(1))
        except (InvalidOperation, ValueError):
            return default

    # 3. Tab/newline separated text with standalone numeric tokens (e.g. 'Freight\t750\t0')
    tokens = re.split(r"[\t\n\r]+", cleaned)
    for tok in tokens:
        tok = tok.strip()
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", tok):
            try:
                return Decimal(tok)
            except (InvalidOperation, ValueError):
                continue

    # Reject alphanumeric identifiers (M40, DN50, 6205-2RS, 4SQMM, M8 x 40, 6 sq mm, etc.)
    return default


def normalize_uom(val: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """Normalizes raw UOM strings into standardized canonical codes."""
    if not val:
        return default
    cleaned = val.lower().strip().rstrip(".")
    # Direct match or without period
    if cleaned in UOM_MAP:
        return UOM_MAP[cleaned]
    raw_clean = val.lower().strip()
    if raw_clean in UOM_MAP:
        return UOM_MAP[raw_clean]
    return val.upper().strip() if val else default


def normalize_currency(val: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """Normalizes currency symbols/strings into ISO codes."""
    if not val:
        return default
    cleaned = val.lower().strip()
    return CURRENCY_MAP.get(cleaned, val.upper().strip() if len(val) == 3 else default)


def parse_flexible_date(val: Optional[str]) -> Optional[date]:
    """Parses various date string formats into datetime.date."""
    if not val or not str(val).strip():
        return None
    val_str = str(val).strip()
    try:
        is_day_first = bool(re.search(r"^\d{1,2}[-/\.]\d{1,2}[-/\.]\d{2,4}", val_str))
        dt = date_parser.parse(val_str, dayfirst=is_day_first)
        return dt.date()
    except (ValueError, OverflowError):
        return None


def detect_tax_rate(text: str) -> Optional[Decimal]:
    """Extracts explicit tax or GST percentage from text snippets."""
    if not text:
        return None
    match = re.search(r"(?:gst|tax|igst|cgst\+sgst)[\s#.:/-]*(\d+(?:\.\d+)?)%", text, re.IGNORECASE)
    if match:
        return Decimal(match.group(1))
    
    if "gst" in text.lower() or "tax" in text.lower():
        match_pct = re.search(r"(\d+(?:\.\d+)?)%", text)
        if match_pct:
            return Decimal(match_pct.group(1))

    return None
