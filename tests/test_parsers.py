from pathlib import Path
import openpyxl
import pytest
from parsers.excel_parser import ExcelParser
from parsers.pdf_parser import PDFParser


@pytest.fixture
def sample_excel_quote(tmp_path: Path) -> Path:
    """Creates a realistic synthetic supplier quotation in Excel format."""
    file_path = tmp_path / "supplier_abc_quote.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quotation"

    ws.append(["ABC Industrial Supplies Pvt Ltd"])
    ws.append(["Quotation Ref:", "QT-2026-8891", "Date:", "2026-08-15"])
    ws.append(["Payment Terms:", "30 Days Credit", "Delivery:", "Ex-Works 7 Days"])
    ws.append([])

    ws.append(["Item Code", "Description", "HSN", "Qty", "UOM", "Rate", "Tax %"])
    ws.append(["SKF-6205", "SKF Deep Groove Ball Bearing 6205-2RS", "8482", 25, "Nos", 520.00, 18.0])
    ws.append(["BOLT-M8-30", "SS304 Hex Bolt M8x30", "7318", 100, "PCS", 4.50, 18.0])
    ws.append(["OIL-SEAL-25", "Nitrile Oil Seal 25x47x7", "8484", 10, "Pieces", 85.00, 18.0])

    wb.save(file_path)
    return file_path


@pytest.fixture
def sample_pdf_quote(tmp_path: Path) -> Path:
    """Creates a realistic synthetic supplier quotation in PDF format."""
    file_path = tmp_path / "xyz_fasteners_quote.pdf"
    pdf_bytes = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 300 >>
stream
BT
/F1 12 Tf
72 712 Td
(XYZ Fasteners Pvt Ltd) Tj
0 -20 Td
(Quote Ref: QT-PDF-901  Date: 2026-08-18) Tj
0 -30 Td
(Description  Qty  UOM  Rate  Tax) Tj
0 -20 Td
(M10x50 High Tensile Bolt  50  PCS  12.50  18%) Tj
0 -20 Td
(Nylon Lock Nut M10  50  PCS  3.00  18%) Tj
ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000228 00000 n 
0000000578 00000 n 
trailer
<< /Size 6 /Root 1 0 R >>
startxref
655
%%EOF
"""
    with open(file_path, "wb") as f:
        f.write(pdf_bytes)
    return file_path


def test_excel_parser_extracts_ast(sample_excel_quote: Path):
    parser = ExcelParser()
    ast = parser.parse(sample_excel_quote)

    assert ast.file_type == "EXCEL"
    assert ast.source_file_name == "supplier_abc_quote.xlsx"
    assert len(ast.source_file_hash) == 64
    assert len(ast.tables) >= 1

    table = ast.tables[0]
    assert "Description" in table.headers
    assert "Rate" in table.headers
    assert len(table.rows) == 3
    assert "SKF Deep Groove Ball Bearing 6205-2RS" in table.rows[0].cells


def test_pdf_parser_extracts_ast(sample_pdf_quote: Path):
    parser = PDFParser()
    ast = parser.parse(sample_pdf_quote)

    assert ast.file_type == "PDF"
    assert ast.source_file_name == "xyz_fasteners_quote.pdf"
    assert len(ast.source_file_hash) == 64
    assert len(ast.tables) >= 1

    table = ast.tables[0]
    assert len(table.rows) == 2
