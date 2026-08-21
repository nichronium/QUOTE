"""
Semantic Table & Region Classifier.
Classifies extracted tables and regions into functional semantic types
(MAIN_LINE_ITEMS, VOLUME_PRICING, SUMMARY_TOTALS, TERMS, HISTORICAL_REFERENCE, etc.)
and ranks candidate tables across all sheets for primary quote selection.
"""

from dataclasses import dataclass
import re
from typing import Dict, List, Optional, Tuple

from extraction.semantic_registry import COLUMN_ALIASES, TableType
from parsers.base import ExtractedTable


class TableClassifier:
    """Classifies tables using header semantics, column counts, value patterns, and structural evidence."""

    TIER_HEADER_KEYWORDS = ["min qty", "max qty", "from qty", "to qty", "tier", "volume pricing", "price slab", "qty from", "qty to"]
    SUMMARY_KEYWORDS = ["subtotal", "sub total", "grand total", "total amount", "tax amount", "round off"]
    TERMS_KEYWORDS = ["commercial terms", "terms and conditions", "payment terms", "delivery terms", "bank details", "bank account"]
    HISTORICAL_KEYWORDS = ["historical", "legacy", "previous price", "old quote", "do not treat", "system export"]
    CHARGE_KEYWORDS = ["freight", "packing", "insurance", "tooling", "loading", "surcharge", "miscellaneous charge"]
    DISCOUNT_KEYWORDS = ["discount slab", "discount bracket", "rebate table", "discount schedule"]
    TAX_KEYWORDS_TABLE = ["tax slab", "tax schedule", "gst rate", "hst table", "vat schedule"]
    METADATA_KEYWORDS = ["quotation details", "document details", "company details", "vendor info", "seller info"]

    def classify_table(self, table: ExtractedTable) -> Tuple[TableType, float]:
        """
        Classifies table into TableType with confidence score.
        Uses combined evidence: headers, value patterns, row structure, and context.
        Returns: (TableType, confidence_score)
        """
        headers = [h.lower().strip() for h in table.headers if h]
        headers_str = " ".join(headers)
        sheet_name = (table.sheet_name or "").lower()

        if not headers and not table.rows:
            return TableType.UNKNOWN, 0.0

        # 1. Historical / Decoy Table Check
        if any(kw in headers_str or kw in sheet_name for kw in self.HISTORICAL_KEYWORDS):
            return TableType.HISTORICAL_REFERENCE, 0.90

        # 2. Volume Pricing / Tier Table Check
        tier_hits = sum(1 for kw in self.TIER_HEADER_KEYWORDS if kw in headers_str or kw in sheet_name)
        if tier_hits >= 2 or any(h in ["min qty", "from qty", "tier", "max qty"] for h in headers):
            return TableType.VOLUME_PRICING, 0.95

        # 3. Charges Table (freight/packing/insurance in rows, not columns)
        if any(kw in headers_str for kw in ["particulars", "description"]):
            # Check if data rows contain charge keywords (not product descriptions)
            row_texts = [r.raw_text.lower() for r in table.rows if r.raw_text]
            charge_row_count = sum(
                1 for rt in row_texts
                if any(kw in rt for kw in self.CHARGE_KEYWORDS)
            )
            if len(row_texts) > 0 and charge_row_count / len(row_texts) >= 0.5:
                return TableType.CHARGES, 0.85

        # 4. Discount Table Check
        if any(kw in headers_str for kw in self.DISCOUNT_KEYWORDS):
            return TableType.DISCOUNT_TABLE, 0.85

        # 5. Tax Table Check
        if any(kw in headers_str for kw in self.TAX_KEYWORDS_TABLE):
            return TableType.TAX_TABLE, 0.85

        # 6. Commercial Terms & Charges Table Check
        if any(kw in headers_str or kw in sheet_name for kw in self.TERMS_KEYWORDS):
            if "commercial terms" in headers_str or "terms_charges" in sheet_name or "terms and conditions" in headers_str:
                return TableType.TERMS, 0.90

        # 7. Metadata Block (key-value pairs with small column count)
        if len(headers) <= 2 and any(kw in headers_str for kw in self.METADATA_KEYWORDS):
            return TableType.METADATA, 0.80

        # 8. Summary / Totals Table Check
        summary_hits = sum(1 for kw in self.SUMMARY_KEYWORDS if kw in headers_str)
        if summary_hits >= 2:
            return TableType.SUMMARY_TOTALS, 0.90

        # 9. Main Line Items Check (multi-signal evidence)
        has_desc = any(self._matches_alias("description", h) for h in headers)
        has_price = any(self._matches_alias("unit_price", h) for h in headers)
        has_qty = any(self._matches_alias("qty", h) for h in headers)
        has_sku = any(self._matches_alias("part_number", h) for h in headers)
        has_uom = any(self._matches_alias("uom", h) for h in headers)
        has_amount = any(self._matches_alias("amount", h) for h in headers)
        has_tax = any(self._matches_alias("tax", h) for h in headers)

        item_signals = sum([has_desc, has_price, has_qty, has_sku, has_uom, has_amount, has_tax])

        if has_desc and (has_price or has_qty):
            confidence = 0.95 if item_signals >= 3 else 0.75
            return TableType.MAIN_LINE_ITEMS, confidence

        if item_signals >= 2 and len(table.rows) >= 1:
            return TableType.MAIN_LINE_ITEMS, 0.65

        return TableType.UNKNOWN, 0.30

    def score_main_item_table(self, table: ExtractedTable) -> float:
        """Computes a priority score for selecting the primary line items table across sheets."""
        t_type, t_conf = self.classify_table(table)
        if t_type != TableType.MAIN_LINE_ITEMS:
            return 0.0

        score = 10.0 * t_conf
        score += min(20.0, len(table.rows) * 1.5)  # More valid product rows = higher priority

        headers = [h.lower().strip() for h in table.headers if h]
        for field_name in ["description", "unit_price", "qty", "uom", "part_number", "discount", "tax", "amount"]:
            if any(self._matches_alias(field_name, h) for h in headers):
                score += 2.0

        # Penalize if sheet name indicates alternate/decoy/reference
        sheet_name = (table.sheet_name or "").lower()
        if any(kw in sheet_name for kw in ["alt", "copy", "old", "backup", "reference", "decoy"]):
            score -= 5.0

        return score

    def _matches_alias(self, field_name: str, header: str) -> bool:
        patterns = COLUMN_ALIASES.get(field_name, [])
        for pat in patterns:
            if re.search(r"\b" + pat + r"\b", header, re.IGNORECASE) or pat in header:
                return True
        return False

