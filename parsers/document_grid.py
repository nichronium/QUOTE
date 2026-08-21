"""
DocumentGrid -- Unified Cell-Level Grid Abstraction.

Provides a rich, format-agnostic grid representation for XLSX and CSV documents.
Preserves: coordinates, data types, number formats, merged ranges, hidden flags,
formula presence/string, native table refs, and full source provenance.

Unlike DocumentAST (which loses cell coordinates after table detection), DocumentGrid
retains every cell's raw position so that downstream components can:
  - Detect repeated-metadata columns (same value across all item rows)
  - Enforce strict region boundaries without row-number heuristics
  - Provide precise field-level provenance (sheet, cell_ref, range)
  - Feed the RegionSegmentor with density and type information per cell
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple



class CellValueType(str, Enum):
    EMPTY = "EMPTY"
    NUMERIC = "NUMERIC"
    TEXT = "TEXT"
    DATE = "DATE"
    BOOLEAN = "BOOLEAN"
    FORMULA = "FORMULA"


@dataclass
class GridCell:
    """Single cell with full provenance and type information."""
    row: int
    col: int
    cell_ref: str
    raw_value: Any
    display_value: str
    value_type: CellValueType
    number_format: Optional[str] = None
    is_merged: bool = False
    merge_ref: Optional[str] = None
    is_hidden_row: bool = False
    is_hidden_col: bool = False
    has_formula: bool = False
    formula_str: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return self.value_type == CellValueType.EMPTY or not self.display_value.strip()

    @property
    def is_numeric(self) -> bool:
        return self.value_type in (CellValueType.NUMERIC, CellValueType.DATE)


@dataclass
class MergedRange:
    ref: str
    min_row: int
    max_row: int
    min_col: int
    max_col: int
    top_left_value: str


@dataclass
class GridSheet:
    """A single sheet/tab or CSV as a 2D grid of cells."""
    sheet_name: str
    sheet_index: int
    cells: List[List[GridCell]]
    max_row: int
    max_col: int
    merged_ranges: List[MergedRange] = field(default_factory=list)
    hidden_row_indices: Set[int] = field(default_factory=set)
    hidden_col_indices: Set[int] = field(default_factory=set)
    formula_count: int = 0
    warnings: List[str] = field(default_factory=list)

    def get_cell(self, row: int, col: int) -> Optional[GridCell]:
        r_idx, c_idx = row - 1, col - 1
        if 0 <= r_idx < len(self.cells) and 0 <= c_idx < len(self.cells[r_idx]):
            return self.cells[r_idx][c_idx]
        return None

    def get_row(self, row: int) -> List[GridCell]:
        if 1 <= row <= self.max_row:
            return self.cells[row - 1]
        return []

    def get_column(self, col: int) -> List[GridCell]:
        result = []
        for row_idx in range(self.max_row):
            row = self.cells[row_idx]
            if col - 1 < len(row):
                result.append(row[col - 1])
        return result

    def row_as_strings(self, row: int, skip_hidden: bool = True) -> List[str]:
        if skip_hidden and row in self.hidden_row_indices:
            return []
        cells = self.get_row(row)
        return [c.display_value for c in cells]

    def visible_rows(self) -> List[int]:
        return [r for r in range(1, self.max_row + 1) if r not in self.hidden_row_indices]

    @property
    def density(self) -> float:
        if self.max_row == 0 or self.max_col == 0:
            return 0.0
        total = self.max_row * self.max_col
        filled = sum(1 for row in self.cells for cell in row if not cell.is_empty)
        return filled / total if total > 0 else 0.0


@dataclass
class DocumentGrid:
    """Complete cell-level grid document representation."""
    source_file: str
    source_file_hash: str
    file_type: str
    sheets: List[GridSheet] = field(default_factory=list)
    encoding: Optional[str] = None
    delimiter: Optional[str] = None

    @property
    def primary_sheet(self) -> Optional[GridSheet]:
        if not self.sheets:
            return None
        return max(self.sheets, key=lambda s: s.density)


def col_index_to_letter(col: int) -> str:
    result = ""
    while col > 0:
        col, remainder = divmod(col - 1, 26)
        result = chr(65 + remainder) + result
    return result


def make_cell_ref(row: int, col: int) -> str:
    return f"{col_index_to_letter(col)}{row}"


class XlsxGridBuilder:
    """Builds a DocumentGrid from an XLSX workbook."""

    def build(self, file_path: Path, file_hash: str) -> DocumentGrid:
        import openpyxl
        wb_data = openpyxl.load_workbook(filename=file_path, data_only=True)
        try:
            wb_formulas = openpyxl.load_workbook(filename=file_path, data_only=False)
        except Exception:
            wb_formulas = None

        grid = DocumentGrid(source_file=str(file_path), source_file_hash=file_hash, file_type="XLSX")

        for sheet_idx, sheet_name in enumerate(wb_data.sheetnames, start=1):
            ws_data = wb_data[sheet_name]
            ws_formulas = wb_formulas[sheet_name] if wb_formulas and sheet_name in wb_formulas.sheetnames else None
            grid.sheets.append(self._build_sheet(ws_data, ws_formulas, sheet_name, sheet_idx))

        return grid

    def _build_sheet(self, ws_data, ws_formulas, sheet_name: str, sheet_idx: int) -> GridSheet:
        max_row = ws_data.max_row or 0
        max_col = ws_data.max_column or 0

        if max_row == 0 or max_col == 0:
            return GridSheet(sheet_name=sheet_name, sheet_index=sheet_idx, cells=[], max_row=0, max_col=0, warnings=["Empty sheet"])

        merged_map: Dict[Tuple[int, int], MergedRange] = {}
        merged_ranges = []
        for rng in ws_data.merged_cells.ranges:
            top_left = ws_data.cell(row=rng.min_row, column=rng.min_col).value
            tl_str = str(top_left).strip() if top_left is not None else ""
            mr = MergedRange(ref=str(rng), min_row=rng.min_row, max_row=rng.max_row,
                             min_col=rng.min_col, max_col=rng.max_col, top_left_value=tl_str)
            merged_ranges.append(mr)
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    merged_map[(r, c)] = mr

        hidden_rows: Set[int] = set()
        hidden_cols: Set[int] = set()
        try:
            for row_dim in ws_data.row_dimensions.values():
                if row_dim.hidden:
                    hidden_rows.add(row_dim.index)
        except Exception:
            pass
        try:
            from openpyxl.utils import column_index_from_string
            for col_dim in ws_data.column_dimensions.values():
                if col_dim.hidden:
                    hidden_cols.add(column_index_from_string(col_dim.index))
        except Exception:
            pass

        formula_count = 0
        cells_2d: List[List[GridCell]] = []

        for r in range(1, max_row + 1):
            row_cells: List[GridCell] = []
            for c in range(1, max_col + 1):
                cell_data = ws_data.cell(row=r, column=c)
                cell_formula = ws_formulas.cell(row=r, column=c) if ws_formulas else None

                raw_val = cell_data.value
                is_merged = (r, c) in merged_map
                merge_ref = merged_map[(r, c)].ref if is_merged else None
                if is_merged and (r, c) != (merged_map[(r, c)].min_row, merged_map[(r, c)].min_col):
                    raw_val = merged_map[(r, c)].top_left_value or raw_val

                has_formula = False
                formula_str = None
                if cell_formula is not None:
                    fval = cell_formula.value
                    if fval is not None and str(fval).startswith("="):
                        has_formula = True
                        formula_str = str(fval)
                        formula_count += 1

                display_val = self._to_display(raw_val, cell_data)
                value_type = self._infer_type(raw_val, has_formula, cell_data)

                gc = GridCell(
                    row=r, col=c, cell_ref=make_cell_ref(r, c),
                    raw_value=raw_val, display_value=display_val, value_type=value_type,
                    number_format=cell_data.number_format if hasattr(cell_data, "number_format") else None,
                    is_merged=is_merged, merge_ref=merge_ref,
                    is_hidden_row=(r in hidden_rows), is_hidden_col=(c in hidden_cols),
                    has_formula=has_formula, formula_str=formula_str
                )
                row_cells.append(gc)
            cells_2d.append(row_cells)

        return GridSheet(
            sheet_name=sheet_name, sheet_index=sheet_idx,
            cells=cells_2d, max_row=max_row, max_col=max_col,
            merged_ranges=merged_ranges,
            hidden_row_indices=hidden_rows, hidden_col_indices=hidden_cols,
            formula_count=formula_count
        )

    def _to_display(self, raw_val: Any, cell) -> str:
        if raw_val is None:
            return ""
        from datetime import datetime, date
        if isinstance(raw_val, (datetime, date)):
            return str(raw_val.date() if isinstance(raw_val, datetime) else raw_val)
        return str(raw_val).strip()

    def _infer_type(self, raw_val: Any, has_formula: bool, cell) -> CellValueType:
        if raw_val is None:
            return CellValueType.EMPTY
        from datetime import datetime, date
        if isinstance(raw_val, bool):
            return CellValueType.BOOLEAN
        if isinstance(raw_val, (int, float)):
            return CellValueType.NUMERIC
        if isinstance(raw_val, (datetime, date)):
            return CellValueType.DATE
        if hasattr(cell, "number_format") and cell.number_format:
            nf = (cell.number_format or "").lower()
            if any(d in nf for d in ["yy", "mm", "dd"]):
                return CellValueType.DATE
        if has_formula:
            return CellValueType.FORMULA
        return CellValueType.TEXT


class CsvGridBuilder:
    """Builds a DocumentGrid from a CSV file. No external encoding library required."""

    COMMON_DELIMITERS = [",", "\t", ";", "|"]

    def build(self, file_path: Path, file_hash: str) -> DocumentGrid:
        raw_bytes = file_path.read_bytes()
        encoding = self._detect_encoding(raw_bytes)
        try:
            text = raw_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            text = raw_bytes.decode("latin-1")
            encoding = "latin-1"

        delimiter = self._detect_delimiter(text)
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        while rows and not any(c.strip() for c in rows[-1]):
            rows.pop()

        if not rows:
            sheet = GridSheet(sheet_name="Sheet1", sheet_index=1, cells=[], max_row=0, max_col=0, warnings=["Empty CSV"])
            return DocumentGrid(source_file=str(file_path), source_file_hash=file_hash, file_type="CSV",
                                sheets=[sheet], encoding=encoding, delimiter=delimiter)

        max_col = max(len(r) for r in rows)
        cells_2d: List[List[GridCell]] = []
        for r_idx, row_data in enumerate(rows, start=1):
            row_cells = []
            for c_idx in range(1, max_col + 1):
                raw_str = row_data[c_idx - 1].strip() if c_idx - 1 < len(row_data) else ""
                raw_val: Any = raw_str
                value_type = self._infer_csv_type(raw_str)
                if value_type == CellValueType.NUMERIC:
                    try:
                        raw_val = float(raw_str.replace(",", ""))
                    except ValueError:
                        pass
                gc = GridCell(row=r_idx, col=c_idx, cell_ref=make_cell_ref(r_idx, c_idx),
                              raw_value=raw_val, display_value=raw_str, value_type=value_type)
                row_cells.append(gc)
            cells_2d.append(row_cells)

        sheet = GridSheet(sheet_name="Sheet1", sheet_index=1, cells=cells_2d, max_row=len(rows), max_col=max_col)
        return DocumentGrid(source_file=str(file_path), source_file_hash=file_hash, file_type="CSV",
                            sheets=[sheet], encoding=encoding, delimiter=delimiter)

    def _detect_encoding(self, raw_bytes: bytes) -> str:
        """
        Stdlib-only encoding detection cascade:
        1. BOM-detected UTF-8-SIG
        2. Try strict UTF-8
        3. Fall back to latin-1 (always succeeds for arbitrary bytes)
        """
        if raw_bytes.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        try:
            raw_bytes.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            return "latin-1"

    def _detect_delimiter(self, text: str) -> str:
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="".join(self.COMMON_DELIMITERS))
            return dialect.delimiter
        except csv.Error:
            first_lines = sample.split("\n")[:5]
            counts = {d: sum(line.count(d) for line in first_lines) for d in self.COMMON_DELIMITERS}
            return max(counts, key=counts.get)


    def _infer_csv_type(self, val: str) -> CellValueType:
        if not val.strip():
            return CellValueType.EMPTY
        cleaned = val.strip().replace(",", "")
        if re.fullmatch(r"[-+]?\d*\.?\d+", cleaned):
            return CellValueType.NUMERIC
        if re.fullmatch(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}", val.strip()):
            return CellValueType.DATE
        return CellValueType.TEXT

