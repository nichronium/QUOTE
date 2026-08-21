"""
Document Profiler.
Analyzes full workbook/file topology, dimensions, cell density, data type distributions,
and produces a structured DocumentProfile before semantic extraction.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import openpyxl

from parsers.base import CellDataType, DocumentAST, DocumentPage


@dataclass
class SheetProfile:
    sheet_name: str
    sheet_index: int
    total_rows: int
    total_cols: int
    non_empty_cells: int
    density_ratio: float
    numeric_count: int
    text_count: int
    quotation_likelihood_score: float
    candidate_region_count: int = 0


@dataclass
class DocumentProfile:
    source_file_name: str
    file_type: str
    sheets: List[SheetProfile] = field(default_factory=list)
    primary_sheet_name: Optional[str] = None
    has_multiple_sheets: bool = False
    warnings: List[str] = field(default_factory=list)


class DocumentProfiler:
    """Profiles document structure, topology, and sheet quotation likelihood."""

    @staticmethod
    def profile_ast(ast: DocumentAST) -> DocumentProfile:
        sheets: List[SheetProfile] = []
        best_sheet = None
        highest_score = -1.0

        for page in ast.pages:
            non_empty = sum(len([c for c in r.cells if c.strip()]) for t in page.tables for r in t.rows)
            for b in page.text_blocks:
                non_empty += len(b.text.split())

            score = page.page_score
            sheet_prof = SheetProfile(
                sheet_name=page.sheet_name or f"Page {page.page_number}",
                sheet_index=page.page_number,
                total_rows=len(page.tables[0].rows) if page.tables else len(page.text_blocks),
                total_cols=len(page.tables[0].headers) if page.tables else 1,
                non_empty_cells=non_empty,
                density_ratio=min(1.0, non_empty / 100.0) if non_empty else 0.0,
                numeric_count=0,
                text_count=non_empty,
                quotation_likelihood_score=score,
                candidate_region_count=len(page.tables)
            )
            sheets.append(sheet_prof)

            if score > highest_score:
                highest_score = score
                best_sheet = sheet_prof.sheet_name

        return DocumentProfile(
            source_file_name=ast.source_file_name,
            file_type=ast.file_type,
            sheets=sheets,
            primary_sheet_name=best_sheet,
            has_multiple_sheets=len(sheets) > 1
        )
