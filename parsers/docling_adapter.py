"""
Docling Document Parser Adapter.
Provides a clean, encapsulated boundary between IBM Docling and Quote Intelligence's
intermediate DocumentAST representation.

Converts Docling layout-aware document models, bounding boxes, and complex table grids
into standard DocumentAST, ExtractedTable, TableRow, and TextBlock structures without
leaking Docling types to downstream extraction or domain models.
"""

import importlib
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

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

logger = logging.getLogger(__name__)


def is_docling_available() -> bool:
    """Checks whether the docling package is installed and importable."""
    try:
        importlib.import_module("docling.document_converter")
        return True
    except (ImportError, ModuleNotFoundError, Exception):
        return False


class DoclingUnavailableError(Exception):
    """Raised when Docling is requested but not available in the environment."""
    pass


class DoclingAdapter(BaseDocumentParser):
    """
    Adapter that converts Docling DocumentConverter output into the project's
    standard intermediate DocumentAST representation.
    """

    def __init__(self):
        self._docling_available = is_docling_available()

    @property
    def is_available(self) -> bool:
        return self._docling_available

    def parse(self, file_path: Path) -> DocumentAST:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Document file not found: {file_path}")

        if not self._docling_available:
            raise DoclingUnavailableError(
                'Docling package is not installed. Install with: pip install "docling>=2.0.0"'
            )

        try:
            from docling.document_converter import DocumentConverter
            import docling
            docling_version = getattr(docling, "__version__", "2.x")
        except Exception as e:
            raise DoclingUnavailableError(f"Failed to import Docling: {e}") from e

        converter = DocumentConverter()
        conv_result = converter.convert(str(file_path.resolve()))
        doc = conv_result.document

        file_hash = compute_file_hash(file_path)
        pages: List[DocumentPage] = []
        all_tables: List[ExtractedTable] = []
        warnings: List[str] = []

        # 1. Extract Pages and Text Blocks from Docling Document
        page_texts: Dict[int, List[TextBlock]] = {}
        page_raw_strings: Dict[int, List[str]] = {}

        if hasattr(doc, "iterate_items"):
            for item, level in doc.iterate_items():
                text_content = getattr(item, "text", "") or ""
                text_content = text_content.strip()
                if not text_content:
                    continue

                page_no = 1
                bbox_tuple: Optional[Tuple[float, float, float, float]] = None

                if hasattr(item, "prov") and item.prov:
                    prov = item.prov[0] if isinstance(item.prov, list) and item.prov else item.prov
                    page_no = getattr(prov, "page_no", 1) or 1
                    raw_bbox = getattr(prov, "bbox", None)
                    if raw_bbox:
                        if hasattr(raw_bbox, "l") and hasattr(raw_bbox, "t"):
                            bbox_tuple = (float(raw_bbox.l), float(raw_bbox.t), float(raw_bbox.r), float(raw_bbox.b))
                        elif isinstance(raw_bbox, (tuple, list)) and len(raw_bbox) == 4:
                            bbox_tuple = (float(raw_bbox[0]), float(raw_bbox[1]), float(raw_bbox[2]), float(raw_bbox[3]))

                if page_no not in page_texts:
                    page_texts[page_no] = []
                    page_raw_strings[page_no] = []

                page_texts[page_no].append(
                    TextBlock(
                        text=text_content,
                        page_number=page_no,
                        bbox=bbox_tuple,
                        is_header_metadata=(page_no == 1 and len(page_texts[page_no]) < 8)
                    )
                )
                page_raw_strings[page_no].append(text_content)

        # 2. Extract Tables from Docling Document
        doc_tables = getattr(doc, "tables", []) or []
        for tbl_idx, doc_tbl in enumerate(doc_tables):
            extracted = self._convert_docling_table(doc_tbl, tbl_idx + 1)
            if extracted:
                all_tables.append(extracted)

        # 3. Assemble DocumentPages
        max_page = max(page_texts.keys()) if page_texts else 1
        if hasattr(doc, "pages") and doc.pages:
            max_page = max(max_page, len(doc.pages))

        for p_idx in range(1, max_page + 1):
            t_blocks = page_texts.get(p_idx, [])
            p_tables = [t for t in all_tables if t.page_number == p_idx]
            raw_p_text = "\n".join(page_raw_strings.get(p_idx, []))
            
            pages.append(
                DocumentPage(
                    page_number=p_idx,
                    text_blocks=t_blocks,
                    tables=p_tables,
                    raw_text=raw_p_text,
                    page_score=0.9 if p_tables else 0.5
                )
            )

        return DocumentAST(
            source_file_name=file_path.name,
            source_file_path=str(file_path.resolve()),
            source_file_hash=file_hash,
            file_type="PDF",
            pages=pages,
            tables=all_tables,
            metadata={
                "parser_used": "docling",
                "docling_version": docling_version,
                "page_count": len(pages),
                "table_count": len(all_tables),
                "warnings": warnings
            }
        )

    def _convert_docling_table(self, doc_tbl: Any, table_idx: int) -> Optional[ExtractedTable]:
        """Converts a Docling table structure into an ExtractedTable AST object."""
        try:
            if hasattr(doc_tbl, "export_to_dataframe"):
                try:
                    df = doc_tbl.export_to_dataframe()
                    if df is not None and not df.empty:
                        headers = [str(c).strip() for c in df.columns]
                        table_rows: List[TableRow] = []
                        page_no = 1
                        if hasattr(doc_tbl, "prov") and doc_tbl.prov:
                            prov = doc_tbl.prov[0] if isinstance(doc_tbl.prov, list) else doc_tbl.prov
                            page_no = getattr(prov, "page_no", 1) or 1

                        for r_idx, row in df.iterrows():
                            cells = [str(v).strip() if v is not None and str(v) != "nan" else "" for v in row.values]
                            raw_line = " ".join(cells)
                            cell_objects = [
                                CellData(
                                    raw_value=v,
                                    normalized_str=cells[c_idx],
                                    row_idx=int(r_idx),
                                    col_idx=c_idx,
                                    data_type=CellDataType.NUMERIC if re.fullmatch(r"[-+]?\d*\.?\d+", re.sub(r"[₹$€,\s%]", "", cells[c_idx])) else CellDataType.TEXT
                                )
                                for c_idx, v in enumerate(row.values)
                            ]
                            table_rows.append(
                                TableRow(
                                    cells=cells,
                                    cell_objects=cell_objects,
                                    raw_text=raw_line,
                                    page_number=page_no,
                                    row_index=int(r_idx)
                                )
                            )

                        return ExtractedTable(
                            headers=headers,
                            rows=table_rows,
                            page_number=page_no,
                            confidence=0.95,
                            table_score=0.95
                        )
                except Exception:
                    pass

            if hasattr(doc_tbl, "data") and hasattr(doc_tbl.data, "grid"):
                grid = doc_tbl.data.grid
                if not grid:
                    return None

                page_no = 1
                if hasattr(doc_tbl, "prov") and doc_tbl.prov:
                    prov = doc_tbl.prov[0] if isinstance(doc_tbl.prov, list) else doc_tbl.prov
                    page_no = getattr(prov, "page_no", 1) or 1

                raw_grid: List[List[str]] = []
                for row in grid:
                    row_cells = [getattr(cell, "text", "") or "" for cell in row]
                    raw_grid.append([c.strip() for c in row_cells])

                if not raw_grid:
                    return None

                headers = raw_grid[0]
                table_rows = []
                for r_idx, row_cells in enumerate(raw_grid[1:], start=1):
                    raw_line = " ".join(row_cells)
                    cell_objects = [
                        CellData(
                            raw_value=c,
                            normalized_str=c,
                            row_idx=r_idx,
                            col_idx=c_idx,
                            data_type=CellDataType.NUMERIC if re.fullmatch(r"[-+]?\d*\.?\d+", re.sub(r"[₹$€,\s%]", "", c)) else CellDataType.TEXT
                        )
                        for c_idx, c in enumerate(row_cells)
                    ]
                    table_rows.append(
                        TableRow(
                            cells=row_cells,
                            cell_objects=cell_objects,
                            raw_text=raw_line,
                            page_number=page_no,
                            row_index=r_idx
                        )
                    )

                return ExtractedTable(
                    headers=headers,
                    rows=table_rows,
                    page_number=page_no,
                    confidence=0.90,
                    table_score=0.90
                )

        except Exception as e:
            logger.warning(f"Error parsing Docling table: {e}")
            return None

        return None
