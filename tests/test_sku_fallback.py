"""
Supplier SKU Fallback Regression Tests.
Verifies:
1. Leading codes (e.g. DYN-01, SS-M8-40, CAB-6SQ) are inferred as numbers when SKU column is missing.
2. Original raw_description is preserved unchanged.
3. FieldStatus.INFERRED is assigned with description_prefix_regex method.
4. Negative cases (ordinary words, quantities, dates, prices) are NEVER inferred as SKUs.
"""

from decimal import Decimal
import io, openpyxl
from pathlib import Path
import pytest
from core.canonical_quote import FieldStatus
from extraction.extractor import QuoteExtractor
from parsers.excel_parser import ExcelParser

def _extract_items(descriptions, tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Quotation'
    ws.append(['Item Description', 'Qty', 'Unit Price', 'GST %'])
    for d in descriptions:
        ws.append([d, 10, 100.0, 18.0])

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fpath = tmp_path / 'sku_fallback_test.xlsx'
    fpath.write_bytes(bio.read())

    ast = ExcelParser().parse(fpath)
    quote = QuoteExtractor().extract(ast)
    return quote.items



def test_positive_sku_prefix_inferred(tmp_path):
    descs = [
        'DYN-01 Precision Ball Screw 25mm',
        'SS-M88-40 Stainless Steel Hex Socket Cap Screw',
        'CAB-6SQ Copper Armoured Flexible Cable',
        'DSNU-25-50 Round Cylinder 50mm'
    ]
    items = _extract_items(descs, tmp_path)
    assert len(items) == 4

    assert items[0].supplier_part_number == 'DYN-01'
    assert items[0].raw_description == 'DYN-01 Precision Ball Screw 25mm'
    assert items[0].supplier_part_number_evidence.status == FieldStatus.INFERRED
    assert items[0].supplier_part_number_evidence.extraction_method == 'description_prefix_regex'

    assert items[1].supplier_part_number == 'SS-M88-40'
    assert items[2].supplier_part_number == 'CAB-6SQ'
    assert items[3].supplier_part_number == 'DSNU-25-50'


def test_negative_sku_not_inferred_for_ordinary_words_quantities_dates(tmp_path):
    descs = [
        '100 Nos Stainless Steel Bolts',                # Quantity + UOM
        '2026-08-15 Delivery Item Safety Goggles',      # Date
        'Precision Ball Screw 25mm',                     # Ordinary description
        'High Quality Steel Pipe 100mm'                  # Ordinary description
    ]
    items = _extract_items(descs, tmp_path)
    assert len(items) == 4

    for item in items:
        assert item.supplier_part_number is None, f'False positive SKU inferred for: {item.raw_description}'
        assert item.supplier_part_number_evidence is None
