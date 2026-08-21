from pathlib import Path
import pytest
from parsers.csv_parser import CSVParser


def test_csv_parser_comma_separated(tmp_path: Path):
    csv_file = tmp_path / "quote_standard.csv"
    csv_content = """# Supplier: Bharat Industrial Hardware Ltd
# Quote Ref: QT-BIH-2026-101
# Date: 2026-08-19

Item Code,Description,Qty,UOM,Rate,Tax %
SKF-6205,"Deep Groove Ball Bearing, 6205-2RS",50,NOS,480.00,18%
SS-M8-30,"Hex Bolt M8x30 SS304, Full Thread",200,PCS,4.25,18%
"""
    with open(csv_file, "w", encoding="utf-8") as f:
        f.write(csv_content)

    parser = CSVParser()
    ast = parser.parse(csv_file)

    assert ast.file_type == "CSV"
    assert len(ast.tables) == 1
    table = ast.tables[0]
    assert len(table.headers) == 6
    assert "Description" in table.headers
    assert len(table.rows) == 2

    # Check cell objects
    row0 = table.rows[0]
    assert row0.cells[0] == "SKF-6205"
    assert row0.cells[1] == "Deep Groove Ball Bearing, 6205-2RS"  # Handled comma inside quotes!
    assert row0.cell_objects[2].raw_value == "50"
    assert row0.cell_objects[2].data_type == "NUMERIC"


def test_csv_parser_with_utf8_bom(tmp_path: Path):
    csv_file = tmp_path / "quote_bom.csv"
    csv_content = """Item Code\tDescription\tQty\tRate
B-01\tNut M10\t100\t2.50
"""
    # Write with UTF-8 BOM
    with open(csv_file, "w", encoding="utf-8-sig") as f:
        f.write(csv_content)

    parser = CSVParser()
    ast = parser.parse(csv_file)
    assert ast.file_type == "CSV"
    assert len(ast.tables) == 1
    assert ast.tables[0].headers[0] == "Item Code"  # Cleanly stripped BOM without garbage characters
