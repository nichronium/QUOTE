"""
Base Parser & Intermediate Document AST.
Rich, format-agnostic intermediate representation of parsed document content.
Preserves both raw values and normalized values for evidence-based extraction.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def compute_file_hash(file_path: Path) -> str:
    """Computes SHA-256 hash of a given file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()


class CellDataType(str, Enum):
    EMPTY = "EMPTY"
    NUMERIC = "NUMERIC"
    TEXT = "TEXT"
    DATE = "DATE"
    BOOLEAN = "BOOLEAN"


class RowType(str, Enum):
    HEADER = "HEADER"
    ITEM = "ITEM"
    SUBTOTAL = "SUBTOTAL"
    GRAND_TOTAL = "GRAND_TOTAL"
    TAX = "TAX"
    FREIGHT = "FREIGHT"
    PACKING = "PACKING"
    INSURANCE = "INSURANCE"
    DISCOUNT = "DISCOUNT"
    NOTE = "NOTE"
    TERMS = "TERMS"
    VOLUME_TIER = "VOLUME_TIER"
    EMPTY = "EMPTY"
    CONTINUATION = "CONTINUATION"
    UNKNOWN = "UNKNOWN"


@dataclass
class CellData:
    """Stores cell content with both raw and normalized representation."""
    raw_value: Any
    normalized_str: str = ""
    row_idx: int = 0
    col_idx: int = 0
    data_type: CellDataType = CellDataType.TEXT

    def __post_init__(self):
        if not self.normalized_str:
            self.normalized_str = str(self.raw_value).strip() if self.raw_value is not None else ""


@dataclass
class TextBlock:
    """A block of raw text with optional page/bbox provenance."""
    text: str
    page_number: Optional[int] = None
    sheet_name: Optional[str] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    is_header_metadata: bool = False


@dataclass
class TableRow:
    """A single row in an extracted table."""
    cells: List[str] = field(default_factory=list)
    cell_objects: List[CellData] = field(default_factory=list)
    raw_text: str = ""
    page_number: Optional[int] = None
    sheet_name: Optional[str] = None
    row_index: int = 0
    row_type: RowType = RowType.UNKNOWN
    is_hidden: bool = False

    def __post_init__(self):
        if not self.cell_objects and self.cells:
            self.cell_objects = [
                CellData(raw_value=c, normalized_str=str(c), row_idx=self.row_index, col_idx=i)
                for i, c in enumerate(self.cells)
            ]
        elif self.cell_objects and not self.cells:
            self.cells = [c.normalized_str for c in self.cell_objects]


@dataclass
class ExtractedTable:
    """A tabular data block extracted from a document with structural scoring."""
    headers: List[str] = field(default_factory=list)
    rows: List[TableRow] = field(default_factory=list)
    page_number: Optional[int] = None
    sheet_name: Optional[str] = None
    confidence: float = 1.0
    table_score: float = 0.0
    col_types: List[str] = field(default_factory=list)
    bounding_box: Optional[Tuple[int, int, int, int]] = None


@dataclass
class DocumentPage:
    """Page-level container for text blocks and tables."""
    page_number: int
    sheet_name: Optional[str] = None
    text_blocks: List[TextBlock] = field(default_factory=list)
    tables: List[ExtractedTable] = field(default_factory=list)
    raw_text: str = ""
    page_score: float = 0.0


@dataclass
class DocumentAST:
    """Complete format-agnostic intermediate document tree."""
    source_file_name: str
    source_file_path: str
    source_file_hash: str
    file_type: str
    pages: List[DocumentPage] = field(default_factory=list)
    tables: List[ExtractedTable] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    grid: Optional[Any] = None


    @property
    def full_text(self) -> str:
        return "\n\n".join(page.raw_text for page in self.pages if page.raw_text)


class BaseDocumentParser(ABC):
    """Abstract base class for all file parsers."""

    @abstractmethod
    def parse(self, file_path: Path) -> DocumentAST:
        pass
