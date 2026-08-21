from datetime import date
from decimal import Decimal
from pathlib import Path
import openpyxl
import pytest

from core.canonical_quote import CanonicalQuote
from extraction.extractor import QuoteExtractor
from parsers.excel_parser import ExcelParser
from parsers.pdf_parser import PDFParser


@pytest.fixture
def sample_excel_file(tmp_path: Path) -> Path:
    file_path = tmp_path / "abc_industries_quote.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    ws.append(["ABC Industrial Supplies Pvt Ltd"])
    ws.append(["Quote No:", "QT-2026-8891", "Dated:", "2026-08-15"])
    ws.append(["Payment Terms:", "30 Days Credit"])
    ws.append([])

    ws.append(["Item Code", "Description", "HSN", "Qty", "UOM", "Rate", "Tax %"])
    ws.append(["SKF-6205", "SKF Deep Groove Ball Bearing 6205-2RS", "8482", 25, "Nos", 520.00, 18.0])
    ws.append(["BOLT-M8-30", "SS304 Hex Bolt M8x30", "7318", 100, "PCS", 4.50, 18.0])
    ws.append(["OIL-SEAL-25", "Nitrile Oil Seal 25x47x7", "8484", 10, "Pieces", 85.00, 18.0])

    wb.save(file_path)
    return file_path


@pytest.fixture
def sample_pdf_file(tmp_path: Path) -> Path:
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


def test_end_to_end_excel_extraction_to_canonical_quote(sample_excel_file: Path):
    parser = ExcelParser()
    ast = parser.parse(sample_excel_file)

    extractor = QuoteExtractor()
    quote = extractor.extract(ast)

    assert isinstance(quote, CanonicalQuote)
    assert "ABC Industrial Supplies" in quote.supplier_raw_name
    assert quote.quote_number == "QT-2026-8891"
    assert quote.quote_date == date(2026, 8, 15)
    assert quote.currency == "INR"
    assert quote.payment_terms is not None
    assert quote.payment_terms.credit_days == 30

    assert len(quote.items) == 3

    # Check Item 0
    item0 = quote.items[0]
    assert item0.raw_description == "SKF Deep Groove Ball Bearing 6205-2RS"
    assert item0.quoted_qty == Decimal("25")
    assert item0.quoted_uom == "PCS"  # Normalized from 'Nos'
    assert item0.unit_price == Decimal("520.00")
    assert item0.tax_rate_pct == Decimal("18.0")
    assert item0.calculate_line_landed_cost() == Decimal("15340.00")

    # Check Item 1
    item1 = quote.items[1]
    assert item1.raw_description == "SS304 Hex Bolt M8x30"
    assert item1.quoted_qty == Decimal("100")
    assert item1.quoted_uom == "PCS"
    assert item1.unit_price == Decimal("4.50")
    assert item1.calculate_line_landed_cost() == Decimal("531.00")

    # Check Item 2
    item2 = quote.items[2]
    assert item2.raw_description == "Nitrile Oil Seal 25x47x7"
    assert item2.quoted_qty == Decimal("10")
    assert item2.quoted_uom == "PCS"  # Normalized from 'Pieces'
    assert item2.unit_price == Decimal("85.00")
    assert item2.calculate_line_landed_cost() == Decimal("1003.00")

    # Check Total Landed Cost: 15340 + 531 + 1003 = 16874.00
    assert quote.calculate_total_landed_cost() == Decimal("16874.00")


def test_end_to_end_pdf_extraction_to_canonical_quote(sample_pdf_file: Path):
    parser = PDFParser()
    ast = parser.parse(sample_pdf_file)

    extractor = QuoteExtractor()
    quote = extractor.extract(ast)

    assert isinstance(quote, CanonicalQuote)
    assert "XYZ Fasteners" in quote.supplier_raw_name
    assert quote.quote_number == "QT-PDF-901"
    assert quote.quote_date == date(2026, 8, 18)
    assert quote.currency == "INR"

    assert len(quote.items) == 2
    assert quote.items[0].raw_description == "M10x50 High Tensile Bolt"
    assert quote.items[0].quoted_qty == Decimal("50")
    assert quote.items[0].unit_price == Decimal("12.50")
    assert quote.items[0].calculate_line_landed_cost() == Decimal("737.50")  # 50 * 12.50 * 1.18 = 737.50

    assert quote.items[1].raw_description == "Nylon Lock Nut M10"
    assert quote.items[1].quoted_qty == Decimal("50")
    assert quote.items[1].unit_price == Decimal("3.00")
    assert quote.items[1].calculate_line_landed_cost() == Decimal("177.00")  # 50 * 3.00 * 1.18 = 177.00

    # Total Landed Cost: 737.50 + 177.00 = 914.50
    assert quote.calculate_total_landed_cost() == Decimal("914.50")
