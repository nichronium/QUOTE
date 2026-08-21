"""
CSV Document Parser.
Deterministic, lightweight parser for quotation CSV files supporting UTF-8, BOM,
delimiter autodetection, quoted multiline cells, and metadata block separation.
"""

import csv
import io
from pathlib import Path
import re
from typing import Any, List, Optional, Tuple

from extraction.region_segmentor import RegionSegmentor
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
from parsers.document_grid import CsvGridBuilder


class CSVParser(BaseDocumentParser):
    """Parses CSV quotation files into DocumentAST using DocumentGrid & RegionSegmentor."""

    def __init__(self):
        self.grid_builder = CsvGridBuilder()
        self.region_segmentor = RegionSegmentor()

    def parse(self, file_path: Path) -> DocumentAST:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"CSV file not found: {file_path}")

        file_hash = compute_file_hash(file_path)

        # 1. Build rich cell-level DocumentGrid
        doc_grid = self.grid_builder.build(file_path, file_hash)

        # 2. Run Region Segmentation on CSV grid sheet
        grid_sheet = doc_grid.sheets[0] if doc_grid.sheets else None
        logical_regions = self.region_segmentor.segment(grid_sheet) if grid_sheet else []

        # Read content using utf-8-sig to automatically strip UTF-8 BOM if present
        with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
            content = f.read()

        delimiter = self._detect_delimiter(content)
        reader = csv.reader(io.StringIO(content), delimiter=delimiter)
        raw_rows = [row for row in reader if any(cell.strip() for cell in row)]

        text_blocks, table = self._extract_table_and_metadata(raw_rows)

        tables_list = [table] if table else []
        page = DocumentPage(
            page_number=1,
            sheet_name="CSV",
            text_blocks=text_blocks,
            tables=tables_list,
            raw_text=content
        )

        return DocumentAST(
            source_file_name=file_path.name,
            source_file_path=str(file_path.resolve()),
            source_file_hash=file_hash,
            file_type="CSV",
            pages=[page],
            tables=tables_list,
            metadata={"delimiter": delimiter, "total_rows": len(raw_rows)},
            grid=doc_grid
        )


    def _detect_delimiter(self, sample_text: str) -> str:
        """Detects delimiter from comma, tab, semicolon, or pipe."""
        if not sample_text:
            return ","
        
        sample_lines = sample_text.strip().split("\n")[:10]
        sample = "\n".join(sample_lines)

        try:
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample, delimiters=",\t;|")
            return dialect.delimiter
        except Exception:
            # Fallback: Count occurrences in header lines
            comma_count = sum(l.count(",") for l in sample_lines)
            tab_count = sum(l.count("\t") for l in sample_lines)
            semi_count = sum(l.count(";") for l in sample_lines)
            pipe_count = sum(l.count("|") for l in sample_lines)

            counts = [(comma_count, ","), (tab_count, "\t"), (semi_count, ";"), (pipe_count, "|")]
            counts.sort(key=lambda x: x[0], reverse=True)
            return counts[0][1] if counts[0][0] > 0 else ","

    def _extract_table_and_metadata(
        self, rows: List[List[str]]
    ) -> Tuple[List[TextBlock], Optional[ExtractedTable]]:
        """Identifies header metadata rows and constructs structured ExtractedTable."""
        if not rows:
            return [], None

        common_header_keywords = [
            "description", "item", "part", "qty", "quantity", "rate", "price", "amount",
            "uom", "unit", "hsn", "sku", "code", "product", "name", "brand", "category", "material"
        ]


        header_idx = -1
        for idx, row in enumerate(rows):
            row_lower = " ".join(c.lower().strip() for c in row)
            match_count = sum(1 for kw in common_header_keywords if kw in row_lower)
            if match_count >= 2:
                header_idx = idx
                break

        text_blocks: List[TextBlock] = []
        if header_idx == -1:
            # If no obvious tabular header, treat all rows as text blocks
            for r_idx, row in enumerate(rows):
                text_blocks.append(TextBlock(text="\t".join(row), page_number=1))
            return text_blocks, None

        # Pre-table rows are metadata blocks
        for r_idx, row in enumerate(rows[:header_idx]):
            text_blocks.append(TextBlock(
                text="\t".join(c.strip() for c in row if c.strip()),
                page_number=1,
                is_header_metadata=True
            ))

        raw_headers = [c.strip() for c in rows[header_idx]]
        # Trim trailing empty headers
        while raw_headers and not raw_headers[-1]:
            raw_headers.pop()

        num_cols = len(raw_headers)
        data_rows: List[TableRow] = []

        for r_idx, row in enumerate(rows[header_idx + 1:], start=header_idx + 1):
            trimmed_row = [c.strip() for c in row[:num_cols]]
            if len(trimmed_row) < num_cols:
                trimmed_row += [""] * (num_cols - len(trimmed_row))

            cell_objects: List[CellData] = []
            for c_idx, cell_str in enumerate(trimmed_row):
                cell_type = self._infer_cell_type(cell_str)
                cell_objects.append(CellData(
                    raw_value=cell_str,
                    normalized_str=cell_str,
                    row_idx=r_idx,
                    col_idx=c_idx,
                    data_type=cell_type
                ))

            data_rows.append(TableRow(
                cells=trimmed_row,
                cell_objects=cell_objects,
                raw_text="\t".join(trimmed_row),
                page_number=1,
                sheet_name="CSV",
                row_index=r_idx
            ))

        table = ExtractedTable(
            headers=raw_headers,
            rows=data_rows,
            page_number=1,
            sheet_name="CSV",
            confidence=0.98,
            table_score=1.0
        )

        return text_blocks, table

    def _infer_cell_type(self, cell_val: str) -> CellDataType:
        """Infers basic data type from string token."""
        if not cell_val or not cell_val.strip():
            return CellDataType.EMPTY
        cleaned = re.sub(r"[₹$€,\s%]", "", cell_val)
        if re.fullmatch(r"[-+]?\d*\.?\d+", cleaned):
            return CellDataType.NUMERIC
        return CellDataType.TEXT
