"""
Topological Sheet Scoring & No Hardcoded Decoy Sheet Tests.
Proves:
1. Sheets with names containing technical, backend, benchmark are STILL
   extracted if they contain valid item tables or charges.
2. A notes/costing sheet with 0 structural item table is rejected due to structure, not name.
3. Extraction never uses a sheet-name keyword blocklist.
"""

from decimal import Decimal
import io, openpyxl
from pathlib import Path
import pytest
from extraction.extractor import QuoteExtractor
from parsers.excel_parser import ExcelParser

def test_sheet_named_technical_backend_still_extracted_if_structural(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Technical_Backend_Specs'
    ws.append(['Valve Manufacturer Ltd', '', '', ''])
    ws.append(['Description', 'Qty', 'Unit Price', 'GST %'])
    ws.append(['Aviation Grade Ball Valve 2-inch', 5, 18500.0, 18.0])
    ws.append(['High Pressure Check Valve', 10, 5200.0, 18.0])
    ws.append([])
    ws.append(['Freight Charge', '', 500.0, ''])

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fpath = tmp_path / 'technical_backend_quote.xlsx'
    fpath.write_bytes(bio.read())

    ast = ExcelParser().parse(fpath)
    quote = QuoteExtractor().extract(ast)

    assert len(quote.items) == 2
    assert quote.items[0].raw_description == 'Aviation Grade Ball Valve 2-inch'
    assert quote.items[0].unit_price == Decimal('18500.00')
    assert quote.items[1].raw_description == 'High Pressure Check Valve'
    assert len(quote.additional_charges) >= 1


def test_low_relevance_notes_sheet_rejected_by_structure(tmp_path):
    wb = openpyxl.Workbook()
    ws1 = wb.active

    ws1.title = 'Quotation'
    ws1.append(['Description', 'Qty', 'Unit Price', 'GST %'])
    ws1.append(['Precision Steel Pin 8mm', 200, 12.50, 18.0])

    ws2 = wb.create_sheet(' Internal Costing')
    ws2.append(['Notes: This is an unstructured text block'])
    ws2.append(['Allocated overhead: 400' ])

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fpath = tmp_path / 'multi_sheet_costing.xlsx'
    fpath.write_bytes(bio.read())

    ast = ExcelParser().parse(fpath)
    quote = QuoteExtractor().extract(ast)

    assert len(quote.items) == 1
    assert quote.items[0].raw_description == 'Precision Steel Pin 8mm'
    assert quote.items[0].provenance.sheet_name == 'Quotation'
