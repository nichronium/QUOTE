"""
Matching Package for RFQ and Item Master Reconciliation.
Provides multi-signal candidate matching, dimension-safe UOM conversion, and non-destructive downstream results.
"""

from matching.models import (
    ItemMasterRecord,
    MatchCandidate,
    MatchedQuoteItem,
    MatchMethod,
    MatchStatus,
    RFQLineItem,
    UOMConversionResult,
)
from matching.uom_resolver import UOMResolver
from matching.matcher import ItemMatcher

__all__ = [
    "ItemMasterRecord",
    "MatchCandidate",
    "MatchedQuoteItem",
    "MatchMethod",
    "MatchStatus",
    "RFQLineItem",
    "UOMConversionResult",
    "UOMResolver",
    "ItemMatcher",
]
