"""
Semantic Column & Row Classifier.
Classifies table columns and rows using dual-layer evidence:
1. Header semantic keywords from central semantic registry with pattern specificity weighting
2. Column value type distribution validation
3. Row structure (Headers vs Items vs Subtotals vs Surcharges vs Tier Tables vs Notes vs Continuations)
"""

from decimal import Decimal
import re
from typing import Any, Dict, List, Optional, Tuple

from extraction.semantic_registry import COLUMN_ALIASES, STANDARD_UOM_SET
from parsers.base import CellDataType, ExtractedTable, RowType, TableRow


class ColumnClassifier:
    """Classifies table columns based on header semantics and statistical value validation."""

    SPECIFICITY_WEIGHTS = {
        "supplier_part_number": 2.5,
        "manufacturer_part_number": 2.5,
        "internal_sku": 2.5,
        "cgst": 2.0,
        "sgst": 2.0,
        "igst": 2.0,
        "cess": 2.0,
        "tax": 2.0,
        "part_number": 1.8,
        "unit_price": 1.8,
        "discount": 1.8,
        "amount": 1.6,
        "description": 1.5,
        "qty": 1.5,
        "uom": 1.5,
        "hsn": 1.5,
        "supplier": 1.0,
        "quote_number": 1.2,
        "quote_date": 1.2,
        "currency": 1.2,
    }

    def classify_table_columns(self, table: ExtractedTable) -> Dict[str, int]:
        headers = table.headers
        col_map: Dict[str, int] = {}
        col_scores: Dict[str, float] = {}

        if not headers:
            return col_map

        num_cols = len(headers)
        rows_data = [r.cells for r in table.rows if r.cells]

        for col_idx, header in enumerate(headers):
            h_clean = header.lower().strip()
            if not h_clean:
                continue

            best_type_for_col: Optional[str] = None
            best_score_for_col = 0.0

            for col_type, patterns in COLUMN_ALIASES.items():
                match_strength = 0.0
                weight = self.SPECIFICITY_WEIGHTS.get(col_type, 1.0)

                # --- Negative Anti-Collision Precedence Guards ---

                # Guard 1: Generic supplier metadata must NEVER match identifier headers
                if col_type == "supplier":
                    if any(re.search(r"\b" + kw + r"\b", h_clean) or kw in h_clean for kw in ["part", "sku", "code", "no", "num", "#", "item", "mfg", "pn", "model"]):
                        continue

                # Guard 2: Description must NEVER match identifier headers
                if col_type == "description":
                    if any(re.search(r"\b" + kw + r"\b", h_clean) for kw in ["item code", "item no", "item #", "item num", "part no", "part #", "part number", "part code", "sku", "mfg", "pn"]):
                        continue

                # Guard 3: Generic part_number must defer to specific compound identifiers
                if col_type == "part_number":
                    if any(re.search(r"\b" + kw + r"\b", h_clean) for kw in ["supplier", "vendor", "seller", "manufacturer", "mfg", "oem", "internal", "our", "buyer"]):
                        continue

                # Guard 4: Unit price must not match tax/gst rate headers
                if col_type == "unit_price":
                    if any(re.search(r"\b" + kw + r"\b", h_clean) for kw in ["tax", "gst", "vat", "cgst", "sgst", "igst", "cess"]):
                        continue

                # Test patterns for this col_type with word boundary
                for pat in patterns:
                    if re.search(r"\b" + pat + r"\b", h_clean, re.IGNORECASE):
                        match_strength = 1.0
                        break

                if match_strength > 0:
                    values = [r[col_idx] for r in rows_data if col_idx < len(r) and r[col_idx].strip()]
                    val_score = self._validate_column_values(col_type, values, h_clean)
                    total_score = match_strength * weight * val_score

                    if total_score > 0.2 and total_score > best_score_for_col:
                        best_score_for_col = total_score
                        best_type_for_col = col_type

            if best_type_for_col and best_score_for_col > col_scores.get(best_type_for_col, 0.0):
                col_map[best_type_for_col] = col_idx
                col_scores[best_type_for_col] = best_score_for_col

        # Fallback for description if not explicitly found
        if "description" not in col_map:
            for col_idx in range(num_cols):
                if col_idx not in col_map.values():
                    values = [r[col_idx] for r in rows_data if col_idx < len(r) and r[col_idx].strip()]
                    if self._is_mostly_text(values):
                        col_map["description"] = col_idx
                        break

        return col_map

    def _validate_column_values(self, col_type: str, values: List[str], header: str = "") -> float:
        """
        Validates column values match expected type distribution.

        Anti-confusion guards:
        - If header could match 'uom' BUT majority of values are numeric -> score 0.0.
        - If header matches 'supplier' BUT values are alphanumeric product codes/SKUs -> score 0.0.
        - If identifier columns contain alphanumeric codes -> score 1.0.
        """
        if not values:
            return 0.5

        numeric_matches = sum(1 for v in values if self._is_numeric(v))
        total_vals = len(values)
        numeric_ratio = numeric_matches / total_vals

        if col_type == "uom":
            # Anti-confusion guard: 'Unit' header with numeric values = NOT uom
            if numeric_ratio > 0.4:
                return 0.0
            uom_match_count = sum(
                1 for v in values
                if v.upper().strip() in STANDARD_UOM_SET
                or v.lower().strip() in ["pcs", "pc", "nos", "mtr", "pair", "set", "roll", "box", "kg", "ft"]
            )
            return 1.0 if (uom_match_count / total_vals) > 0.3 else 0.5

        elif col_type in ["qty", "unit_price", "amount", "discount", "tax", "cgst", "sgst", "igst", "cess", "moq"]:
            if numeric_ratio >= 0.4:
                return 1.0
            return 0.2

        elif col_type in ["supplier_part_number", "manufacturer_part_number", "internal_sku", "part_number"]:
            # Identifiers should contain code-like tokens (alphanumeric, hyphens, slashes, numbers)
            code_matches = sum(1 for v in values if self._looks_like_identifier(v))
            if (code_matches / total_vals) >= 0.4:
                return 1.0
            return 0.7

        elif col_type == "supplier":
            # If values look like model numbers or diverse product SKUs, reject
            code_matches = sum(1 for v in values if self._looks_like_identifier(v))
            if (code_matches / total_vals) > 0.6:
                return 0.0
            if numeric_ratio > 0.7:
                return 0.1
            return 1.0

        elif col_type == "description":
            if numeric_ratio > 0.7:
                return 0.1
            return 1.0

        return 0.9

    def _looks_like_identifier(self, val: str) -> bool:
        v = val.strip()
        if len(v) < 2 or len(v) > 40:
            return False
        has_digit = any(c.isdigit() for c in v)
        has_alpha = any(c.isalpha() for c in v)
        has_punct = any(c in "-_./#" for c in v)
        return (has_digit and has_alpha) or (has_alpha and has_punct) or (has_digit and has_punct)

    def _is_numeric(self, val: str) -> bool:
        cleaned = re.sub(r"[₹$€,\s%]", "", str(val).strip())
        return bool(re.fullmatch(r"[-+]?\d*\.?\d+", cleaned))

    def _is_mostly_text(self, values: List[str]) -> bool:
        if not values:
            return False
        text_count = sum(1 for v in values if not self._is_numeric(v) and len(v.strip()) >= 3)
        return (text_count / len(values)) >= 0.5


class RowClassifier:
    """
    Classifies table rows to filter out headers, subtotals, taxes, freight,
    packing, insurance, notes, tier pricing rows, and empty rows.

    Row types (15):
    HEADER, ITEM, CONTINUATION, SUBTOTAL, GRAND_TOTAL, TAX, DISCOUNT,
    FREIGHT, PACKING, INSURANCE, NOTE, TERMS, VOLUME_TIER, EMPTY, UNKNOWN.
    """

    SUBTOTAL_KEYWORDS = ["subtotal", "sub total", "net total", "gross total", "amount in words", "total (inr)"]
    GRAND_TOTAL_KEYWORDS = ["grand total", "total amount", "invoice total", "final amount", "amount payable"]
    TAX_KEYWORDS = ["gst amount", "cgst amount", "sgst amount", "igst amount", "tax amount", "vat amount"]
    DISCOUNT_KEYWORDS = ["discount amount", "less discount", "special discount", "rebate"]
    FREIGHT_KEYWORDS = ["freight", "transportation", "shipping", "courier", "carriage", "cartage"]
    PACKING_KEYWORDS = ["packing", "packaging", "forwarding", "handling"]
    INSURANCE_KEYWORDS = ["insurance", "transit insurance"]
    NOTE_KEYWORDS = [
        "terms and conditions", "payment terms", "note:", "notes:", "authorized signatory",
        "thank you", "bank account", "ifsc", "account no", "account #", "for any queries",
        "supplier:", "vendor:", "quote details", "quotation details", "commercial offer",
        "quote no:", "quote ref:", "date:", "dated:", "validity:", "delivery:"
    ]
    TERMS_KEYWORDS = ["delivery terms", "payment terms", "validity", "lead time"]
    TIER_KEYWORDS = ["min qty", "max qty", "volume pricing", "tier pricing", "price slab", "from qty", "qty from"]
    HELPER_ROW_KEYWORDS = [
        "internal", "costing", "markup", "margin", "helper", "formula",
        "dummy", "decoy", "test", "benchmark_notes", "backend", "calculation",
        "temp", "hidden internal"
    ]

    def classify_row(self, row: "TableRow", col_map: Dict[str, int]) -> "RowType":
        raw_text = row.raw_text.lower().strip()
        cells = [c.strip() for c in row.cells if c.strip()]

        if not cells:
            return RowType.NOTE  # treat empty as NOTE (filtered out)

        # --- Hidden helper / internal calculation row detection
        if getattr(row, "is_hidden", False):
            if any(kw in raw_text for kw in self.HELPER_ROW_KEYWORDS):
                return RowType.NOTE
            # If zero qty and zero price in hidden row, treat as helper calculation row
            if "qty" in col_map and "unit_price" in col_map:
                q_val = row.cells[col_map["qty"]].strip() if col_map["qty"] < len(row.cells) else ""
                p_val = row.cells[col_map["unit_price"]].strip() if col_map["unit_price"] < len(row.cells) else ""
                if q_val in ["0", "0.0", ""] and p_val in ["0", "0.0", ""]:
                    return RowType.NOTE

        # --- Volume/Tier Rows
        for kw in self.TIER_KEYWORDS:
            if kw in raw_text:
                return RowType.NOTE

        # --- Note / Terminal Section Rows
        for kw in self.NOTE_KEYWORDS:
            if kw in raw_text:
                return RowType.NOTE

        # --- Grand Total (before subtotal, so more-specific match first)
        for kw in self.GRAND_TOTAL_KEYWORDS:
            if re.search(r"\b" + kw + r"\b", raw_text):
                return RowType.SUBTOTAL

        # --- Subtotal Rows
        for kw in self.SUBTOTAL_KEYWORDS:
            if re.search(r"\b" + kw + r"\b", raw_text):
                return RowType.SUBTOTAL

        # --- Freight / Packing / Insurance / Discount (charge rows inside main table)
        for kw in self.FREIGHT_KEYWORDS:
            if re.search(r"\b" + kw + r"\b", raw_text):
                return RowType.FREIGHT

        for kw in self.PACKING_KEYWORDS:
            if re.search(r"\b" + kw + r"\b", raw_text):
                return RowType.PACKING

        for kw in self.INSURANCE_KEYWORDS:
            if re.search(r"\b" + kw + r"\b", raw_text):
                return RowType.PACKING

        for kw in self.DISCOUNT_KEYWORDS:
            if re.search(r"\b" + kw + r"\b", raw_text):
                return RowType.DISCOUNT

        for kw in self.TAX_KEYWORDS:
            if re.search(r"\b" + kw + r"\b", raw_text):
                return RowType.TAX

        # Header check: all cells are strings and match column keywords
        if any(h in raw_text for h in ["description", "unit price", "basic rate", "qty", "quantity", "uom", "part no", "part number", "sku"]):
            return RowType.HEADER

        # --- Continuation Row check:
        # A row is only a continuation if it lacks numeric quantity and numeric price
        # AND lacks its own independent line identity (line number or SKU / part number).
        has_qty = False
        has_price = False
        has_ident = False

        if "qty" in col_map and col_map["qty"] < len(row.cells):
            val = row.cells[col_map["qty"]].strip()
            if val and re.search(r"\d", val):
                has_qty = True

        if "unit_price" in col_map and col_map["unit_price"] < len(row.cells):
            val = row.cells[col_map["unit_price"]].strip()
            if val and re.search(r"\d", val):
                has_price = True

        # Check for explicit identifier (part number / SKU)
        for ident_key in ["supplier_part_number", "manufacturer_part_number", "internal_sku", "part_number"]:
            if ident_key in col_map and col_map[ident_key] < len(row.cells):
                val = row.cells[col_map[ident_key]].strip()
                if val and (self._looks_like_identifier(val) or len(val) >= 2):
                    has_ident = True
                    break

        # Check for line index / item number in first cell or line column
        if not has_ident and len(row.cells) > 0:
            first_cell = row.cells[0].strip()
            if re.fullmatch(r"\d{1,4}\.?", first_cell):
                has_ident = True

        if not has_qty and not has_price and not has_ident:
            return RowType.CONTINUATION

        return RowType.ITEM

    def _looks_like_identifier(self, val: str) -> bool:
        v = val.strip()
        if len(v) < 2 or len(v) > 40:
            return False
        has_digit = any(c.isdigit() for c in v)
        has_alpha = any(c.isalpha() for c in v)
        has_punct = any(c in "-_./#" for c in v)
        return (has_digit and has_alpha) or (has_alpha and has_punct) or (has_digit and has_punct)

