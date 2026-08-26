"""
Normalization Utilities.
Transforms extracted strings into clean, typed canonical representations
(Decimals, Dates, Standardized UOMs, Currencies) using centralized registries.
Ensures numeric tokens are never glued across whitespace or adjacent columns.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Optional
from dateutil import parser as date_parser

from extraction.semantic_registry import CURRENCY_MAP, UOM_MAP


def normalize_decimal(val: Optional[str], default: Optional[Decimal] = None) -> Optional[Decimal]:
    """
    Safely extracts and parses numeric string to Decimal.
    Isolates numeric tokens without stripping whitespace globally across column boundaries.
    """
    if val is None:
        return default
    val_str = str(val).strip()
    if not val_str:
        return default

    # Remove currency symbols and formatting commas between digits
    val_str = re.sub(r"[₹$€£]", "", val_str)
    val_str = re.sub(r"(?<=\d),(?=\d)", "", val_str)

    # Match first standalone numeric token (e.g. 750 in "Freight\t750\t0" or 14219.00 in "14219.00")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", val_str)
    if match:
        try:
            return Decimal(match.group(0))
        except (InvalidOperation, ValueError):
            return default
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
