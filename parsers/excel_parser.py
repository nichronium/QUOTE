"""
Excel Document Parser (Generalized Spreadsheet Understanding).
Performs multi-sheet workbook analysis, merged-cell propagation, hidden row/column inspection,
and strict line-item table detection using TableDetector.
"""

from pathlib import Path
import re
from typing import Any, List, Optional, Tuple
import openpyxl

from extraction.region_segmentor import RegionSegmentor
from extraction.table_detector import TableDetector
from parsers.base import (
    BaseDocumentParser,
    CellData,
    CellDataType,
    DocumentAST,
    DocumentPage,
    ExtractedTable,
    TableRow,
    TextBlock,
    compute_file_hash,
)
from parsers.document_grid import XlsxGridBuilder



class ExcelParser(BaseDocumentParser):
    """Generalized multi-sheet Excel workbook analyzer and table extractor using DocumentGrid & RegionSegmentor."""

    def __init__(self):
        self.table_detector = TableDetector()
        self.grid_builder = XlsxGridBuilder()
        self.region_segmentor = RegionSegmentor()

    def parse(self, file_path: Path) -> DocumentAST:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Excel file not found: {file_path}")

        file_hash = compute_file_hash(file_path)
        
        # 1. Build rich cell-level DocumentGrid
        doc_grid = self.grid_builder.build(file_path, file_hash)

        wb = openpyxl.load_workbook(filename=file_path, data_only=True)

        pages: List[DocumentPage] = []
        all_tables: List[ExtractedTable] = []

        for sheet_idx, sheet_name in enumerate(wb.sheetnames, start=1):
            sheet = wb[sheet_name]
            raw_rows = self._extract_raw_rows_with_merged_cells(sheet)

            if not raw_rows:
                continue

            # 2. Run Region Segmentation on corresponding GridSheet if available
            grid_sheet = doc_grid.sheets[sheet_idx - 1] if sheet_idx - 1 < len(doc_grid.sheets) else None
            logical_regions = self.region_segmentor.segment(grid_sheet) if grid_sheet else []

            # 3. Detect tables and text blocks in sheet rows
            tables_in_sheet, text_blocks, warning_msg = self.table_detector.detect_tables_in_rows(
                raw_rows, sheet_name, sheet_idx
            )

            all_tables.extend(tables_in_sheet)

            page_text = "\n".join(["\t".join(r[1]) for r in raw_rows])
            sheet_score = max((t.table_score for t in tables_in_sheet), default=0.1)

            page = DocumentPage(
                page_number=sheet_idx,
                sheet_name=sheet_name,
                text_blocks=text_blocks,
                tables=tables_in_sheet,
                raw_text=page_text,
                page_score=sheet_score
            )
            pages.append(page)

        all_tables.sort(key=lambda t: t.table_score, reverse=True)

        return DocumentAST(
            source_file_name=file_path.name,
            source_file_path=str(file_path.resolve()),
            source_file_hash=file_hash,
            file_type="EXCEL",
            pages=pages,
            tables=all_tables,
            metadata={"sheet_count": len(wb.sheetnames), "sheet_names": wb.sheetnames},
            grid=doc_grid
        )


    def _extract_raw_rows_with_merged_cells(self, sheet) -> List[Tuple[int, List[str], bool]]:
        """Extracts rows from sheet while resolving merged cell values and tagging hidden rows."""
        max_row = sheet.max_row or 0
        max_col = sheet.max_column or 0
        if max_row == 0 or max_col == 0:
            return []

        # Detect hidden rows
        hidden_rows = set()
        try:
            for row_dim in sheet.row_dimensions.values():
                if row_dim.hidden:
                    hidden_rows.add(row_dim.index)
        except Exception:
            pass

        # Build 2D matrix of cell values
        matrix: List[List[str]] = []
        for r in range(1, max_row + 1):
            row_vals = []
            for c in range(1, max_col + 1):
                val = sheet.cell(row=r, column=c).value
                row_vals.append(str(val).strip() if val is not None else "")
            matrix.append(row_vals)

        # Propagate merged cell top-left values across merged ranges
        for rng in sheet.merged_cells.ranges:
            top_left_val = matrix[rng.min_row - 1][rng.min_col - 1]
            if top_left_val:
                for r in range(rng.min_row, rng.max_row + 1):
                    for c in range(rng.min_col, rng.max_col + 1):
                        if not matrix[r - 1][c - 1]:
                            matrix[r - 1][c - 1] = top_left_val

        raw_rows: List[Tuple[int, List[str], bool]] = []
        for r_idx, row in enumerate(matrix, start=1):
            is_hidden = r_idx in hidden_rows
            if any(row):
                # Trim trailing blank cells
                while row and not row[-1]:
                    row.pop()
                if row:
                    raw_rows.append((r_idx, row, is_hidden))
            else:
                raw_rows.append((r_idx, [], is_hidden))

        return raw_rows



