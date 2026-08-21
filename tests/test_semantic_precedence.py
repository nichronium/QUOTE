"""
Regression Test Suite for Semantic Header Classification Precedence & Identifier Aliases.
Verifies that compound identifiers (Supplier Part Number, Manufacturer PN, etc.) are never
misclassified as generic supplier metadata, and that value distribution validation prevents cross-classification.
"""

from decimal import Decimal
import openpyxl
import pytest

from extraction.classifier import ColumnClassifier
from extraction.extractor import QuoteExtractor
from parsers.base import CellData, CellDataType, ExtractedTable, TableRow
from parsers.excel_parser import ExcelParser


def make_table(headers: list, rows_data: list) -> ExtractedTable:
    rows = []
    for r_idx, r_data in enumerate(rows_data):
        cells = [str(x) for x in r_data]
        cell_objs = [
            CellData(
                row_idx=r_idx,
                col_idx=c_idx,
                raw_value=str(x),
                data_type=CellDataType.TEXT
            )
            for c_idx, x in enumerate(r_data)
        ]
        rows.append(TableRow(row_index=r_idx, cells=cells, cell_objects=cell_objs))
    return ExtractedTable(headers=headers, rows=rows, sheet_name="Sheet1")



def test_supplier_part_number_classification():
    """Case 1: 'Supplier Part Number' -> supplier_part_number (never generic supplier)."""
    classifier = ColumnClassifier()
    table = make_table(
        headers=["Supplier Part Number", "Description", "Quantity", "Unit Price"],
        rows_data=[
            ["SKF-6205-2RS", "Ball Bearing 25x52x15mm", "100", "120.00"],
            ["POL-6SQ-CU", "Armoured Cable 6 Sq.mm", "500", "250.00"],
        ]
    )
    col_map = classifier.classify_table_columns(table)
    assert col_map.get("supplier_part_number") == 0
    assert "supplier" not in col_map


def test_supplier_part_no_classification():
    """Case 2: 'Supplier Part No' -> supplier_part_number."""
    classifier = ColumnClassifier()
    table = make_table(
        headers=["Supplier Part No", "Description", "Qty", "Rate"],
        rows_data=[
            ["SKF-6205-2RS", "Ball Bearing", "10", "120.00"],
            ["SKF-6206-2RS", "Ball Bearing", "20", "160.00"],
        ]
    )
    col_map = classifier.classify_table_columns(table)
    assert col_map.get("supplier_part_number") == 0
    assert "supplier" not in col_map


def test_supplier_sku_classification():
    """Case 3: 'Supplier SKU' -> supplier_part_number."""
    classifier = ColumnClassifier()
    table = make_table(
        headers=["Supplier SKU", "Description", "Qty", "Rate"],
        rows_data=[
            ["SKU-BEAR-001", "Ball Bearing", "10", "120.00"],
        ]
    )
    col_map = classifier.classify_table_columns(table)
    assert col_map.get("supplier_part_number") == 0
    assert "supplier" not in col_map


def test_vendor_part_number_classification():
    """Case 4: 'Vendor Part Number' -> supplier_part_number."""
    classifier = ColumnClassifier()
    table = make_table(
        headers=["Vendor Part Number", "Description", "Qty", "Price"],
        rows_data=[
            ["V-PART-9901", "Pressure Transmitter", "5", "18500.00"],
        ]
    )
    col_map = classifier.classify_table_columns(table)
    assert col_map.get("supplier_part_number") == 0
    assert "supplier" not in col_map


def test_manufacturer_part_number_classification():
    """Case 5: 'Manufacturer Part Number' -> manufacturer_part_number."""
    classifier = ColumnClassifier()
    table = make_table(
        headers=["Manufacturer Part Number", "Description", "Qty", "Price"],
        rows_data=[
            ["6205-2RS1", "Deep Groove Ball Bearing", "100", "115.00"],
        ]
    )
    col_map = classifier.classify_table_columns(table)
    assert col_map.get("manufacturer_part_number") == 0
    assert "supplier" not in col_map


def test_mfg_pn_classification():
    """Case 6: 'Mfg PN' -> manufacturer_part_number."""
    classifier = ColumnClassifier()
    table = make_table(
        headers=["Mfg PN", "Description", "Qty", "Price"],
        rows_data=[
            ["LC1D32M7", "3-Pole AC Contactor", "40", "1620.00"],
        ]
    )
    col_map = classifier.classify_table_columns(table)
    assert col_map.get("manufacturer_part_number") == 0
    assert "supplier" not in col_map


def test_generic_supplier_metadata_classification():
    """Case 7: 'Supplier' -> supplier metadata (when values are supplier names)."""
    classifier = ColumnClassifier()
    table = make_table(
        headers=["Supplier", "Description", "Qty", "Price"],
        rows_data=[
            ["Alpha Industrial Supplies Ltd", "Ball Bearing", "10", "120.00"],
            ["Alpha Industrial Supplies Ltd", "Cable", "500", "250.00"],
        ]
    )
    col_map = classifier.classify_table_columns(table)
    assert col_map.get("supplier") == 0


def test_supplier_name_metadata_classification():
    """Case 8: 'Supplier Name' -> supplier metadata."""
    classifier = ColumnClassifier()
    table = make_table(
        headers=["Supplier Name", "Description", "Qty", "Price"],
        rows_data=[
            ["Bharat Heavy Components Pvt Ltd", "Ball Bearing", "10", "115.00"],
        ]
    )
    col_map = classifier.classify_table_columns(table)
    assert col_map.get("supplier") == 0


def test_workbook_with_both_supplier_and_supplier_part_number(tmp_path):
    """Case 9: Workbook containing BOTH 'Supplier' and 'Supplier Part Number'."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quote"
    ws.append(["Supplier", "Supplier Part Number", "Description", "Quantity", "Unit Price"])
    ws.append(["Alpha Industrial Supplies Ltd", "SKF-6205-2RS", "Ball Bearing 25x52x15mm", 100, 120.0])
    ws.append(["Alpha Industrial Supplies Ltd", "POL-6SQ-CU", "Armoured Cable 6 Sq.mm", 500, 240.0])

    fpath = tmp_path / "both_supplier_and_sku.xlsx"
    wb.save(fpath)

    ast = ExcelParser().parse(fpath)
    quote = QuoteExtractor().extract(ast)

    assert quote.supplier_raw_name == "Alpha Industrial Supplies Ltd"
    assert len(quote.items) == 2
    assert quote.items[0].supplier_part_number == "SKF-6205-2RS"
    assert quote.items[1].supplier_part_number == "POL-6SQ-CU"


def test_workbook_with_multiple_identifier_types_no_cross_classification(tmp_path):
    """Case 10: Workbook containing Supplier, Manufacturer Part Number, Supplier Part Number, SKU."""
    classifier = ColumnClassifier()
    table = make_table(
        headers=["Supplier", "Manufacturer Part Number", "Supplier Part Number", "SKU", "Description", "Unit Price"],
        rows_data=[
            ["Alpha Corp", "6205-2RS1", "SKF-6205-2RS", "BEAR-6205", "Ball Bearing", "120.00"],
            ["Alpha Corp", "DIN-912-M8-40", "SS-M8-40", "FAST-SS-M8-40", "Hex Screw M8x40", "8.50"],
        ]
    )
    col_map = classifier.classify_table_columns(table)

    assert col_map.get("supplier") == 0
    assert col_map.get("manufacturer_part_number") == 1
    assert col_map.get("supplier_part_number") == 2
    assert col_map.get("part_number") == 3
    assert col_map.get("description") == 4
    assert col_map.get("unit_price") == 5
