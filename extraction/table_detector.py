"""
Strict Line-Item Table & Region Detector.
Detects exact tabular boundaries using structural and semantic evidence.
Enforces strict table boundaries and failure-safety without scanning arbitrary cells.
"""

from dataclasses import dataclass, field
from decimal import Decimal
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from extraction.semantic_registry import COLUMN_ALIASES, TableType
from parsers.base import CellData, CellDataType, ExtractedTable, RowType, TableRow, TextBlock


@dataclass
class DetectedTableCandidate:
    table_type: TableType
    header_row_idx: int
    headers: List[str]
    start_data_row_idx: int
    end_data_row_idx: int
    data_rows: List[TableRow] = field(default_factory=list)
    confidence_score: float = 0.0
    detected_columns: Dict[str, int] = field(default_factory=dict)
    is_valid_item_table: bool = False
    rejection_reason: Optional[str] = None


class TableDetector:
    """Detects and validates line-item and tier-pricing tables with strict boundary enforcement."""

    TIER_HEADER_KEYWORDS = [
        "min qty", "max qty", "from qty", "to qty", "tier", "volume pricing", "price slab", "qty from", "qty to"
    ]
    SUBTOTAL_KEYWORDS = [
        "subtotal", "sub total", "grand total", "total amount", "taxable value", "total (inr)", "round off"
    ]
    TERMINAL_DIVIDERS = [
        "terms and conditions", "commercial terms", "payment terms", "bank details", "bank account",
        "ifsc", "branch", "account no", "account #", "cheque", "neft", "rtgs", "beneficiary", "notes",
        "for any queries", "authorized signatory", "authorized signature",
        "freight charges", "freight & shipping", "transit insurance", "wooden packing", "packing & forwarding"
    ]


    def detect_tables_in_rows(
        self,
        raw_rows: List[Tuple[int, List[str]]],
        sheet_name: str,
        sheet_idx: int
    ) -> Tuple[List[ExtractedTable], List[TextBlock], Optional[str]]:
        """
        Detects tables within a sheet's row stream.
        Returns: (tables, text_blocks, error_or_warning_message)
        """
        if not raw_rows:
            return [], [], "Empty sheet"

        # 1. Identify all candidate header rows
        candidates: List[DetectedTableCandidate] = []
        for i, row_entry in enumerate(raw_rows):
            orig_r_idx, row_cells = row_entry[0], row_entry[1]
            candidate = self._evaluate_header_row(row_cells, i)
            if candidate:
                candidates.append(candidate)

        # If no valid headers found, treat all non-empty rows as pure text blocks
        if not candidates:
            text_blocks = [
                TextBlock(text="\t".join(c for c in r[1] if c), page_number=sheet_idx, sheet_name=sheet_name)
                for r in raw_rows
            ]
            return [], text_blocks, "Line-item table could not be confidently identified."

        # Sort candidates chronologically by row index
        candidates.sort(key=lambda c: c.header_row_idx)

        # 2. Extract Pre-Table Metadata Text Blocks
        first_header_pos = candidates[0].header_row_idx
        text_blocks: List[TextBlock] = []
        for row_entry in raw_rows[:first_header_pos]:
            orig_r_idx, r_cells = row_entry[0], row_entry[1]
            line_str = "\t".join(c for c in r_cells if c)
            if line_str:
                text_blocks.append(TextBlock(
                    text=line_str,
                    page_number=sheet_idx,
                    sheet_name=sheet_name,
                    is_header_metadata=True
                ))


        extracted_tables: List[ExtractedTable] = []

        # 3. Extract Each Table Strictly Within Its Boundaries
        for c_idx, candidate in enumerate(candidates):
            h_pos = candidate.header_row_idx
            h_entry = raw_rows[h_pos]
            orig_h_num, h_row = h_entry[0], h_entry[1]

            headers = [c for c in h_row]
            while headers and not headers[-1]:
                headers.pop()

            num_cols = len(headers)
            next_h_pos = candidates[c_idx + 1].header_row_idx if c_idx + 1 < len(candidates) else len(raw_rows)

            data_rows: List[TableRow] = []
            in_data_table = True
            consecutive_blanks = 0

            for row_idx_in_slice in range(h_pos + 1, next_h_pos):
                row_entry = raw_rows[row_idx_in_slice]
                orig_r_num = row_entry[0]
                r_cells = row_entry[1]
                is_hidden = row_entry[2] if len(row_entry) > 2 else False
                row_str_lower = " ".join(c.lower() for c in r_cells if c).strip()

                if not any(r_cells):
                    consecutive_blanks += 1
                    if consecutive_blanks >= 2:
                        in_data_table = False
                    continue

                consecutive_blanks = 0

                if not in_data_table:
                    # Save trailing content as text block
                    text_blocks.append(TextBlock(
                        text="\t".join(c for c in r_cells if c), page_number=sheet_idx, sheet_name=sheet_name
                    ))
                    continue


                # Check for terminal section dividers
                if any(kw in row_str_lower for kw in self.TERMINAL_DIVIDERS):
                    in_data_table = False
                    text_blocks.append(TextBlock(
                        text="\t".join(c for c in r_cells if c), page_number=sheet_idx, sheet_name=sheet_name
                    ))
                    continue

                # Check for subtotal / grand total footer rows
                if any(re.search(r"\b" + kw + r"\b", row_str_lower) for kw in self.SUBTOTAL_KEYWORDS):
                    in_data_table = False
                    text_blocks.append(TextBlock(
                        text="\t".join(c for c in r_cells if c), page_number=sheet_idx, sheet_name=sheet_name
                    ))
                    continue

                aligned = r_cells[:num_cols] if len(r_cells) >= num_cols else r_cells + [""] * (num_cols - len(r_cells))
                if any(aligned):
                    cell_objs = [
                        CellData(
                            raw_value=c,
                            normalized_str=str(c).strip(),
                            row_idx=orig_r_num - 1,
                            col_idx=col_i,
                            data_type=self._infer_type(c)
                        )
                        for col_i, c in enumerate(aligned)
                    ]
                    data_rows.append(TableRow(
                        cells=aligned,
                        cell_objects=cell_objs,
                        raw_text="\t".join(aligned),
                        page_number=sheet_idx,
                        sheet_name=sheet_name,
                        row_index=orig_r_num - 1,
                        is_hidden=is_hidden
                    ))

            table_score = candidate.confidence_score

            extracted_tables.append(ExtractedTable(
                headers=headers,
                rows=data_rows,
                page_number=sheet_idx,
                sheet_name=sheet_name,
                confidence=candidate.confidence_score,
                table_score=table_score
            ))

        return extracted_tables, text_blocks, None

    def _evaluate_header_row(self, row: List[str], row_idx: int) -> Optional[DetectedTableCandidate]:
        """Scores candidate header row against semantic aliases."""
        clean_row = [c.lower().strip() for c in row if c]
        if not clean_row or len(clean_row) < 2:
            return None

        # A header row must NOT be predominantly numeric (data rows have numbers in rate/qty/tax cols)
        numeric_count = sum(1 for c in clean_row if self._is_numeric(c))
        if numeric_count > 0 and (numeric_count / len(clean_row)) >= 0.4:
            return None

        row_str = " ".join(clean_row)

        # 1. Tier Table Header Check
        tier_hits = sum(1 for kw in self.TIER_HEADER_KEYWORDS if kw in row_str)
        if tier_hits >= 2 or any(h in ["min qty", "from qty", "tier"] for h in clean_row):
            return DetectedTableCandidate(
                table_type=TableType.VOLUME_PRICING,
                header_row_idx=row_idx,
                headers=row,
                start_data_row_idx=row_idx + 1,
                end_data_row_idx=row_idx + 1,
                confidence_score=0.95,
                is_valid_item_table=False
            )

        # 2. Main Line Item Table Check
        matched_cols: Dict[str, int] = {}
        for col_idx, cell in enumerate(row):
            c_clean = cell.lower().strip()
            if not c_clean or self._is_numeric(c_clean):
                continue

            for field_name, patterns in COLUMN_ALIASES.items():
                if field_name not in matched_cols:
                    if any(re.search(r"\b" + pat + r"\b", c_clean, re.IGNORECASE) or pat in c_clean for pat in patterns):
                        matched_cols[field_name] = col_idx

        has_desc = "description" in matched_cols or "part_number" in matched_cols
        has_price = "unit_price" in matched_cols or "line_total" in matched_cols
        has_qty = "qty" in matched_cols or "uom" in matched_cols

        score = 0.0
        if has_desc and has_price:
            score = 0.90 if has_qty else 0.75
        elif has_desc and has_qty:
            score = 0.70
        elif has_price and has_qty:
            score = 0.60
        elif len(matched_cols) >= 3:
            score = 0.65

        if score >= 0.50:
            return DetectedTableCandidate(
                table_type=TableType.MAIN_LINE_ITEMS,
                header_row_idx=row_idx,
                headers=row,
                start_data_row_idx=row_idx + 1,
                end_data_row_idx=row_idx + 1,
                confidence_score=score,
                detected_columns=matched_cols,
                is_valid_item_table=True
            )

        return None

    def _is_numeric(self, val: str) -> bool:
        cleaned = re.sub(r"[₹$€,\s%]", "", str(val).strip())
        return bool(re.fullmatch(r"[-+]?\d*\.?\d+", cleaned))

    def _infer_type(self, val: Any) -> CellDataType:
        if val is None or not str(val).strip():
            return CellDataType.EMPTY
        cleaned = re.sub(r"[₹$€,\s%]", "", str(val).strip())
        if re.fullmatch(r"[-+]?\d*\.?\d+", cleaned):
            return CellDataType.NUMERIC
        return CellDataType.TEXT
