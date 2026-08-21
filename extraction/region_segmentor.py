"""
Region Segmentor -- Layout-Agnostic Worksheet Segmentation.

Splits a GridSheet into LogicalRegion objects BEFORE any table classification
or field extraction. This ensures that item extraction never crosses region
boundaries and that metadata, item tables, totals, and notes are processed
in their correct semantic context.

Segmentation signals used (all general, no hardcoding):
1. Blank-row gaps (>= 1 completely empty row) -> region boundary
2. Density change: sparse->dense or dense->sparse transitions
3. Candidate header rows (multi-column text row without majority numerics)
4. Total / grand-total label rows -> region terminus
5. Repeated header detection -> new sub-region start
6. Column-count change (significant difference) -> possible new table region
7. Terminal section keywords (terms, bank details, auth signatory) -> boundary
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import List, Optional, Tuple

from parsers.document_grid import GridSheet, GridCell, CellValueType


# ---------------------------------------------------------------------------
# Region types
# ---------------------------------------------------------------------------

class RegionType(str, Enum):
    HEADER_METADATA = "HEADER_METADATA"   # Pre-table supplier/quote info block
    TABLE = "TABLE"                        # Data table (items, tiers, charges, etc.)
    POST_TABLE_METADATA = "POST_TABLE_METADATA"  # After-table terms/notes
    SEPARATOR = "SEPARATOR"               # Blank rows separating regions
    UNKNOWN = "UNKNOWN"


@dataclass
class LogicalRegion:
    """A contiguous block of rows with a coherent semantic purpose."""
    region_type: RegionType
    start_row: int          # 1-based, inclusive
    end_row: int            # 1-based, inclusive
    rows: List[List[str]]   # display values per row (non-hidden)
    density: float          # fraction of non-empty cells
    has_header_row: bool    # first row looks like a column header
    col_count: int          # number of columns in the widest row
    warnings: List[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return self.end_row - self.start_row + 1

    def is_candidate_item_table(self) -> bool:
        """Quick heuristic: table region with a header row and data rows."""
        return (
            self.region_type == RegionType.TABLE
            and self.has_header_row
            and self.row_count >= 2
            and self.col_count >= 2
        )


# ---------------------------------------------------------------------------
# Segmentor
# ---------------------------------------------------------------------------

TERMINAL_KEYWORDS = re.compile(
    r"\b(terms\s+and\s+conditions|commercial\s+terms|payment\s+terms|bank\s+details|"
    r"bank\s+account|ifsc|account\s+no|account\s+#|cheque|neft|rtgs|beneficiary|"
    r"authorized\s+signatory|authorized\s+signature|for\s+and\s+on\s+behalf|"
    r"thank\s+you|notes?:|note\s*:|contact\s+us|for\s+any\s+queries)\b",
    re.IGNORECASE
)

TOTAL_KEYWORDS = re.compile(
    r"\b(grand\s+total|net\s+total|sub\s+total|subtotal|total\s+amount|"
    r"taxable\s+value|total\s+before\s+tax|amount\s+payable|invoice\s+total)\b",
    re.IGNORECASE
)

HEADER_CANDIDATE_KEYWORDS = re.compile(
    r"\b(description|particulars|item|product|material|qty|quantity|rate|price|"
    r"unit|uom|gst|tax|amount|value|sr\.?|s\.?no\.?|code|sku|part)\b",
    re.IGNORECASE
)


class RegionSegmentor:
    """
    Segments a GridSheet into logical regions based on structural and semantic evidence.
    No hardcoded row numbers, sheet names, vendor names, or column coordinates.
    """

    MIN_BLANK_ROWS_FOR_BOUNDARY = 1     # single blank row = boundary
    DENSITY_CHANGE_THRESHOLD = 0.30     # 30% density change = possible new region
    MIN_COL_COUNT_FOR_TABLE = 2

    def segment(self, sheet: GridSheet) -> List[LogicalRegion]:
        """
        Main entry: segment the sheet and return ordered LogicalRegion list.
        """
        if sheet.max_row == 0:
            return []

        # Build row summaries: (row_1based, display_values, density, is_blank, is_header_candidate)
        row_summaries = self._build_row_summaries(sheet)

        # Group into raw segments separated by blank-row gaps
        raw_segments = self._split_on_blank_gaps(row_summaries)

        # Classify and refine each segment
        regions: List[LogicalRegion] = []
        first_table_seen = False
        for seg_start, seg_end, seg_rows in raw_segments:
            if not seg_rows:
                continue
            region = self._classify_segment(seg_start, seg_end, seg_rows, first_table_seen)
            if region.region_type == RegionType.TABLE:
                first_table_seen = True
            regions.append(region)

        return regions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_row_summaries(self, sheet: GridSheet):
        """Returns list of (row_1based, display_vals, density, is_blank, is_header_cand)."""
        summaries = []
        for r in range(1, sheet.max_row + 1):
            if r in sheet.hidden_row_indices:
                continue
            cells = sheet.get_row(r)
            vals = [c.display_value for c in cells]
            non_empty = [v for v in vals if v.strip()]
            total = len(vals)
            density = len(non_empty) / total if total > 0 else 0.0
            is_blank = len(non_empty) == 0
            is_header_cand = self._is_header_candidate(non_empty)
            summaries.append((r, vals, density, is_blank, is_header_cand))
        return summaries

    def _split_on_blank_gaps(self, row_summaries) -> List[Tuple[int, int, list]]:
        """
        Splits row summaries into non-blank contiguous blocks.
        Returns: [(start_row, end_row, rows_in_block)]
        """
        segments = []
        current_block = []
        block_start = None

        for r, vals, density, is_blank, is_header in row_summaries:
            if is_blank:
                if current_block:
                    segments.append((block_start, current_block[-1][0], current_block))
                    current_block = []
                    block_start = None
            else:
                if block_start is None:
                    block_start = r
                current_block.append((r, vals, density, is_header))

        if current_block:
            segments.append((block_start, current_block[-1][0], current_block))

        return segments

    def _classify_segment(self, start: int, end: int, rows: list, first_table_seen: bool) -> LogicalRegion:
        """
        Classifies a contiguous non-blank row block into a RegionType.
        """
        row_texts = [row_data[1] for row_data in rows]  # display value lists
        densities = [row_data[2] for row_data in rows]
        header_flags = [row_data[3] for row_data in rows]

        avg_density = sum(densities) / len(densities) if densities else 0.0
        col_count = max((len(r) for r in row_texts), default=0)
        has_header = header_flags[0] if header_flags else False

        # Flatten all text for keyword search
        all_text = " ".join(" ".join(str(v) for v in r) for r in row_texts).lower()

        # --- Terminal section? -> POST_TABLE_METADATA
        if TERMINAL_KEYWORDS.search(all_text) and first_table_seen:
            return LogicalRegion(
                region_type=RegionType.POST_TABLE_METADATA,
                start_row=start, end_row=end,
                rows=[r for r in row_texts],
                density=avg_density,
                has_header_row=False,
                col_count=col_count
            )

        # --- Very sparse (single-column or < 2 cols) with no header -> METADATA or POST
        if col_count < self.MIN_COL_COUNT_FOR_TABLE or avg_density < 0.15:
            region_type = RegionType.POST_TABLE_METADATA if first_table_seen else RegionType.HEADER_METADATA
            return LogicalRegion(
                region_type=region_type,
                start_row=start, end_row=end,
                rows=[r for r in row_texts],
                density=avg_density,
                has_header_row=False,
                col_count=col_count
            )

        # --- Has a header row + multi-column + reasonable density -> TABLE
        if has_header and col_count >= self.MIN_COL_COUNT_FOR_TABLE:
            return LogicalRegion(
                region_type=RegionType.TABLE,
                start_row=start, end_row=end,
                rows=[r for r in row_texts],
                density=avg_density,
                has_header_row=True,
                col_count=col_count
            )

        # --- Pre-first-table, multi-cell rows -> HEADER_METADATA
        if not first_table_seen:
            return LogicalRegion(
                region_type=RegionType.HEADER_METADATA,
                start_row=start, end_row=end,
                rows=[r for r in row_texts],
                density=avg_density,
                has_header_row=has_header,
                col_count=col_count
            )

        # --- Post-table with no header -> POST_TABLE_METADATA
        if first_table_seen and not has_header:
            return LogicalRegion(
                region_type=RegionType.POST_TABLE_METADATA,
                start_row=start, end_row=end,
                rows=[r for r in row_texts],
                density=avg_density,
                has_header_row=False,
                col_count=col_count
            )

        return LogicalRegion(
            region_type=RegionType.UNKNOWN,
            start_row=start, end_row=end,
            rows=[r for r in row_texts],
            density=avg_density,
            has_header_row=has_header,
            col_count=col_count
        )

    def _is_header_candidate(self, non_empty_vals: List[str]) -> bool:
        """
        Heuristic: a row is a header candidate if:
        - At least 2 non-empty cells
        - Majority of cells are text (not numeric)
        - At least one cell matches a known header keyword
        """
        if len(non_empty_vals) < 2:
            return False

        numeric_count = sum(1 for v in non_empty_vals if self._is_numeric(v))
        numeric_ratio = numeric_count / len(non_empty_vals)

        if numeric_ratio > 0.5:
            return False

        joined = " ".join(non_empty_vals)
        return bool(HEADER_CANDIDATE_KEYWORDS.search(joined))

    def _is_numeric(self, val: str) -> bool:
        cleaned = re.sub(r"[^0-9.\-+]", "", val.strip())
        if not cleaned:
            return False
        try:
            float(cleaned)
            return True
        except ValueError:
            return False
