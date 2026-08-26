"""
PDF Document Parser (Enhanced).
Extracts text blocks, page structure, and candidate quotation tables from digitally born PDFs.
Provides clear diagnostics and explicit warnings when table structures cannot be reliably segmented.
"""

import logging
from pathlib import Path
import re
from typing import List, Optional, Tuple
import pypdf

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
from parsers.docling_adapter import DoclingAdapter, is_docling_available

logger = logging.getLogger(__name__)


class PDFParser(BaseDocumentParser):
    """
    Parses PDF documents into format-agnostic DocumentAST.
    Delegates to DoclingAdapter as the preferred layout-aware parser when available,
    with deterministic fallback to the legacy pypdf text-heuristic parser.
    """

    COMMON_HEADER_KEYWORDS = [
        "description", "item", "part", "qty", "quantity", "rate", "price",
        "amount", "uom", "unit", "hsn", "sku", "particulars", "specification"
    ]

    def __init__(self, prefer_docling: bool = True):
        self.prefer_docling = prefer_docling

    def parse(self, file_path: Path) -> DocumentAST:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"PDF file not found: {file_path}")

        # 1. Preferred Docling extraction path
        fallback_reason: Optional[str] = None
        if self.prefer_docling and is_docling_available():
            try:
                adapter = DoclingAdapter()
                ast = adapter.parse(file_path)
                return ast
            except Exception as e:
                fallback_reason = f"Docling execution failed: {str(e)}"
                logger.warning(f"{fallback_reason}. Falling back to legacy PDF parser.")
        elif self.prefer_docling:
            fallback_reason = "Docling not installed in environment (optional dependency)"

        # 2. Deterministic Legacy pypdf Parser Fallback
        ast = self._parse_legacy(file_path)
        ast.metadata["parser_used"] = "pypdf_fallback" if (self.prefer_docling and fallback_reason) else "pypdf"
        if fallback_reason:
            ast.metadata["fallback_reason"] = fallback_reason

        return ast

    def _parse_legacy(self, file_path: Path) -> DocumentAST:
        file_hash = compute_file_hash(file_path)
        reader = pypdf.PdfReader(str(file_path))

        pages: List[DocumentPage] = []
        all_tables: List[ExtractedTable] = []
        total_text_length = 0
        warnings: List[str] = []

        for page_idx, page in enumerate(reader.pages, start=1):
            raw_text = page.extract_text() or ""
            total_text_length += len(raw_text.strip())
            lines = [line.strip() for line in raw_text.split("\n") if line.strip()]

            table, non_table_lines = self._extract_table_from_lines(lines, page_idx)
            tables_in_page = [table] if table else []
            if table:
                all_tables.append(table)

            text_blocks = [
                TextBlock(
                    text=line,
                    page_number=page_idx,
                    is_header_metadata=(page_idx == 1 and idx < 8)
                )
                for idx, line in enumerate(non_table_lines)
            ]

            pages.append(DocumentPage(
                page_number=page_idx,
                text_blocks=text_blocks,
                tables=tables_in_page,
                raw_text=raw_text,
                page_score=table.table_score if table else 0.2
            ))

        if total_text_length == 0:
            warnings.append("SCANNED_PDF_NO_TEXT: Document appears to be a scanned image with no embedded text layer; OCR required.")
        elif not all_tables:
            warnings.append("PDF_TABLE_EXTRACTION_UNSUPPORTED: Text was extracted, but tabular line-item structure could not be reliably segmented.")

        return DocumentAST(
            source_file_name=file_path.name,
            source_file_path=str(file_path.resolve()),
            source_file_hash=file_hash,
            file_type="PDF",
            pages=pages,
            tables=all_tables,
            metadata={
                "parser_used": "pypdf",
                "page_count": len(reader.pages),
                "total_text_chars": total_text_length,
                "warnings": warnings
            }
        )

    def _extract_table_from_lines(
        self, lines: List[str], page_idx: int
    ) -> Tuple[Optional[ExtractedTable], List[str]]:
        """Extracts candidate tabular rows using multi-strategy header & numeric line segmentation."""
        if not lines:
            return None, []

        header_idx = -1
        max_matches = 0

        for i, line in enumerate(lines):
            line_lower = line.lower()
            matches = sum(1 for kw in self.COMMON_HEADER_KEYWORDS if kw in line_lower)
            if matches >= 2 and matches > max_matches:
                header_idx = i
                max_matches = matches

        if header_idx == -1:
            # Fallback: Check if there are lines matching tabular quote patterns (Description followed by numbers)
            candidate_rows = self._scan_numeric_item_lines(lines, page_idx)
            if len(candidate_rows) >= 1:
                table = ExtractedTable(
                    headers=["Description", "Qty", "UOM", "Unit Price", "Tax %"],
                    rows=candidate_rows,
                    page_number=page_idx,
                    confidence=0.75,
                    table_score=0.6
                )
                return table, [l for l in lines if l not in [r.raw_text for r in candidate_rows]]
            return None, lines

        raw_header = lines[header_idx]
        headers = [h.strip() for h in re.split(r"\s{2,}|\t", raw_header) if h.strip()]
        if len(headers) < 2:
            headers = [h.strip() for h in raw_header.split() if h.strip()]

        num_cols = len(headers)
        data_rows: List[TableRow] = []
        non_table_lines = lines[:header_idx]

        for r_i, line in enumerate(lines[header_idx + 1:], start=header_idx + 1):
            cells = [c.strip() for c in re.split(r"\s{2,}|\t", line) if c.strip()]
            if len(cells) < 2:
                cells = line.split()

            # Align cells with header length
            aligned_cells = cells[:num_cols] if len(cells) >= num_cols else cells + [""] * (num_cols - len(cells))

            if any(re.search(r"\d+", c) for c in aligned_cells):
                cell_objs = [
                    CellData(
                        raw_value=c,
                        normalized_str=c,
                        row_idx=r_i,
                        col_idx=c_i,
                        data_type=CellDataType.NUMERIC if re.fullmatch(r"[-+]?\d*\.?\d+", re.sub(r"[₹$€,\s%]", "", c)) else CellDataType.TEXT
                    )
                    for c_i, c in enumerate(aligned_cells)
                ]
                data_rows.append(TableRow(
                    cells=aligned_cells,
                    cell_objects=cell_objs,
                    raw_text=line,
                    page_number=page_idx,
                    row_index=r_i
                ))
            else:
                non_table_lines.append(line)

        if not data_rows:
            return None, lines

        table = ExtractedTable(
            headers=headers,
            rows=data_rows,
            page_number=page_idx,
            confidence=0.90 if max_matches >= 3 else 0.70,
            table_score=0.8
        )
        return table, non_table_lines

    def _scan_numeric_item_lines(self, lines: List[str], page_idx: int) -> List[TableRow]:
        """Fallback scanner for un-headered lines matching: <Description> <Qty> <UOM> <Rate>."""
        rows: List[TableRow] = []
        for idx, line in enumerate(lines):
            # Matches text with at least two numbers (e.g. Qty and Price)
            tokens = line.split()
            numeric_tokens = [t for t in tokens if re.search(r"\d+", t)]
            if len(tokens) >= 3 and len(numeric_tokens) >= 2:
                rows.append(TableRow(
                    cells=tokens,
                    raw_text=line,
                    page_number=page_idx,
                    row_index=idx
                ))
        return rows
