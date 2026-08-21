"""
Generalized Spreadsheet Understanding Pipeline Test Suite.
Verifies robust quotation extraction across 21 diverse spreadsheet structures without workbook-specific rules.
"""

from datetime import date
from decimal import Decimal
import io
from pathlib import Path
import openpyxl
from openpyxl.styles import Font, PatternFill, Border, Side
import pytest

from core.canonical_quote import CanonicalQuote, ChargeType
from extraction.extractor import QuoteExtractor
from extraction.reconciliation import QuoteReconciler, ReconciliationStatus, ValidationStatus
from parsers.excel_parser import ExcelParser


def create_workbook_bytes(builder_fn) -> bytes:
    wb = openpyxl.Workbook()
    builder_fn(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_and_extract_bytes(file_bytes: bytes, tmp_path: Path) -> CanonicalQuote:
    test_file = tmp_path / "test_quote.xlsx"
    test_file.write_bytes(file_bytes)
    parser = ExcelParser()
    ast = parser.parse(test_file)
    extractor = QuoteExtractor()
    return extractor.extract(ast)


# =========================================================================
# Scenario 1: Normal Standard Table
# =========================================================================
def test_scenario_01_normal_standard_table(tmp_path):
    def build(wb):
        ws = wb.active
        ws.title = "Quote"
        ws.append(["Supplier: Delta Industrial Tools", "Quote No: DIT-881", "Date: 2026-08-20"])
        ws.append([])
        ws.append(["Item Code", "Description", "Qty", "UOM", "Rate", "GST %", "Total"])
        ws.append(["BRG-100", "Deep Groove Ball Bearing", 10, "PCS", 250.0, 18, 2950.0])
        ws.append(["BLT-200", "High Tensile Bolt M10", 100, "NOS", 15.0, 18, 1770.0])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 2
    assert quote.items[0].raw_description == "Deep Groove Ball Bearing"
    assert quote.items[0].calculate_line_landed_cost() == Decimal("2950.00")
    assert quote.items[1].calculate_line_landed_cost() == Decimal("1770.00")
    assert quote.calculate_total_landed_cost() == Decimal("4720.00")


# =========================================================================
# Scenario 2: Metadata Above Table
# =========================================================================
def test_scenario_02_metadata_above_table(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["VENDOR QUOTATION"])
        ws.append(["Vendor Name:", "Zenith Precision Ltd"])
        ws.append(["Quotation Ref:", "ZPL/2026/99"])
        ws.append(["Quotation Date:", "2026-08-15"])
        ws.append(["Payment Terms:", "45 Days Net"])
        ws.append([])
        ws.append(["Sr.", "Description", "Quantity", "Unit", "Price", "GST %"])
        ws.append([1, "Carbide Endmill 4 Flute", 5, "PCS", 800.0, 18])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert quote.supplier_raw_name == "Zenith Precision Ltd"
    assert quote.quote_number == "ZPL/2026/99"
    assert len(quote.items) == 1
    assert quote.items[0].unit_price == Decimal("800.00")


# =========================================================================
# Scenario 3: Metadata Below Table
# =========================================================================
def test_scenario_03_metadata_below_table(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "Rate", "Tax %"])
        ws.append(["Hydraulic Hose 1/2 inch", 20, 350.0, 18])
        ws.append([])
        ws.append(["Terms & Conditions:"])
        ws.append(["Payment Terms: 30 Days from delivery"])
        ws.append(["Delivery: Ex-stock 3-5 days"])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    assert quote.payment_terms is not None
    assert "30 days" in quote.payment_terms.raw_text.lower()


# =========================================================================
# Scenario 4: Multiple Sheets (Quote on Sheet 2, Decoy on Sheet 1)
# =========================================================================
def test_scenario_04_multiple_sheets_quote_on_sheet_2(tmp_path):
    def build(wb):
        ws1 = wb.active
        ws1.title = "Instructions"
        ws1.append(["Please read the quotation in the next tab."])
        ws1.append(["Contact sales@apex.com for clarifications."])

        ws2 = wb.create_sheet(title="Quotation")
        ws2.append(["Supplier: Apex Tools Pvt Ltd", "Ref: APX-001"])
        ws2.append(["Description", "Qty", "UOM", "Rate", "GST %"])
        ws2.append(["Turning Insert CNMG 120408", 50, "PCS", 120.0, 18])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    assert quote.items[0].raw_description == "Turning Insert CNMG 120408"
    assert quote.supplier_raw_name == "Apex Tools Pvt Ltd"


# =========================================================================
# Scenario 5: Multiple Tables on Same Sheet (Items + Tier Pricing)
# =========================================================================
def test_scenario_05_multiple_tables_same_sheet(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "Rate", "GST %"])
        ws.append(["Copper Cable 2.5 sq mm", 100, 45.0, 18])
        ws.append([])
        ws.append(["VOLUME PRICING TIERS"])
        ws.append(["Min Qty", "Max Qty", "Unit Price"])
        ws.append([1, 99, 45.0])
        ws.append([100, 499, 42.0])
        ws.append([500, "", 38.0])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1  # Tier table rows must NOT become quote items!
    assert len(quote.items[0].price_tiers) == 3
    assert quote.items[0].price_tiers[1].unit_price == Decimal("42.00")


# =========================================================================
# Scenario 6: Merged Cells in Headers and Metadata
# =========================================================================
def test_scenario_06_merged_cells_propagation(tmp_path):
    def build(wb):
        ws = wb.active
        ws.merge_cells("A1:D1")
        ws["A1"] = "Shree Balaji Industrial Corp"
        ws.append([])
        ws.append(["Item Description", "Qty", "Unit Rate", "GST %"])
        ws.append(["Flange Gasket DN50", 25, 65.0, 18])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert quote.supplier_raw_name == "Shree Balaji Industrial Corp"
    assert len(quote.items) == 1
    assert quote.items[0].unit_price == Decimal("65.00")


# =========================================================================
# Scenario 7: Blank Rows Interspersed in Table
# =========================================================================
def test_scenario_07_blank_rows_interspersed(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "Rate", "GST %"])
        ws.append(["Safety Helmet Yellow", 10, 180.0, 18])
        ws.append(["", "", "", ""])  # 1 blank row
        ws.append(["Safety Goggles Clear", 20, 85.0, 18])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 2


# =========================================================================
# Scenario 8: Hidden Rows and Columns
# =========================================================================
def test_scenario_08_hidden_rows_and_columns(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Internal Code", "Qty", "Rate", "GST %"])
        ws.column_dimensions["B"].hidden = True
        ws.append(["Pneumatic Valve 5/2", "INT-999", 4, 1250.0, 18])
        ws.append(["Internal Test Row", "DUMMY", 0, 0, 0])
        ws.row_dimensions[3].hidden = True

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) >= 1
    assert quote.items[0].raw_description == "Pneumatic Valve 5/2"


# =========================================================================
# Scenario 9: Excel Formulas Evaluation
# =========================================================================
def test_scenario_09_formula_cells_evaluation(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "Rate", "Amount"])
        ws.append(["LED Floodlight 50W", 10, 850.0, 8500.0])  # Evaluated formula in data_only

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    assert quote.items[0].unit_price == Decimal("850.00")
    assert quote.items[0].quoted_qty == Decimal("10.00")


# =========================================================================
# Scenario 10: Formatted Empty Cells (Borders on blank cells)
# =========================================================================
def test_scenario_10_formatted_empty_cells(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "Rate", "Tax %"])
        ws.append(["Ball Valve 1 inch", 5, 450.0, 18])
        # Add blank rows with styling
        for _ in range(5):
            ws.append(["", "", "", ""])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1  # Blank styled rows must NOT become items


# =========================================================================
# Scenario 11: Notes and Disclaimers Outside Table
# =========================================================================
def test_scenario_11_notes_outside_table(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Particulars", "Qty", "Basic Price", "GST %"])
        ws.append(["Centrifugal Pump 2HP", 2, 12500.0, 18])
        ws.append([])
        ws.append(["Important Notes:"])
        ws.append(["1. All prices are ex-works."])
        ws.append(["2. Warranty is 12 months from commissioning."])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    assert not any("warranty" in it.raw_description.lower() for it in quote.items)


# =========================================================================
# Scenario 12: Subtotals, Taxes & Grand Total Blocks
# =========================================================================
def test_scenario_12_subtotals_and_taxes_blocks(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "Rate", "GST %"])
        ws.append(["Mild Steel Angle 50x50x6", 100, 65.0, 18]) # 6500 + 1170 = 7670
        ws.append(["Subtotal", "", "", 6500.0])
        ws.append(["GST 18%", "", "", 1170.0])
        ws.append(["Grand Total", "", "", 7670.0])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    assert quote.calculate_total_landed_cost() == Decimal("7670.00")


# =========================================================================
# Scenario 13: Commercial Charges (Freight, Packing, Insurance)
# =========================================================================
def test_scenario_13_commercial_charges(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "Rate", "GST %"])
        ws.append(["Air Filter Element", 8, 320.0, 18])
        ws.append([])
        ws.append(["Freight Charges", 500.0])
        ws.append(["Packing & Forwarding", 200.0])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    charge_types = [c.charge_type for c in quote.additional_charges]
    assert ChargeType.FREIGHT in charge_types
    assert ChargeType.PACKING in charge_types


# =========================================================================
# Scenario 14: Unusual Headers ("Particulars", "Basic Price", "Tariff")
# =========================================================================
def test_scenario_14_unusual_headers(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Tariff", "Particulars", "Units", "Basic Price", "Rebate %", "Tax Rate"])
        ws.append(["8482", "Spherical Roller Bearing", 4, 1850.0, 5, 18])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    assert quote.items[0].raw_description == "Spherical Roller Bearing"
    assert quote.items[0].unit_price == Decimal("1850.00")
    assert quote.items[0].discount_pct == Decimal("5.00")


# =========================================================================
# Scenario 15: Unusual UOMs ("PAIR", "SET", "MTR", "ROLL")
# =========================================================================
def test_scenario_15_unusual_uoms(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "UOM", "Rate", "GST %"])
        ws.append(["Safety Gloves", 50, "Pairs", 95.0, 18])
        ws.append(["Gasket Kit", 10, "SET", 450.0, 18])
        ws.append(["Insulation Tape", 20, "Rolls", 35.0, 18])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 3
    assert quote.items[0].quoted_uom == "PAIR"
    assert quote.items[1].quoted_uom == "SET"
    assert quote.items[2].quoted_uom == "ROLL"


# =========================================================================
# Scenario 16: Multiple Tax Structures (0%, 5%, 12%, 18%, 28%)
# =========================================================================
def test_scenario_16_multiple_tax_structures(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "Rate", "Tax %"])
        ws.append(["Exempt Item", 10, 100.0, 0])
        ws.append(["Low Tax Item", 10, 100.0, 5])
        ws.append(["Medium Tax Item", 10, 100.0, 12])
        ws.append(["Standard Tax Item", 10, 100.0, 18])
        ws.append(["High Tax Item", 10, 100.0, 28])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 5
    assert quote.items[0].tax_rate_pct == Decimal("0.00")
    assert quote.items[1].tax_rate_pct == Decimal("5.00")
    assert quote.items[2].tax_rate_pct == Decimal("12.00")
    assert quote.items[3].tax_rate_pct == Decimal("18.00")
    assert quote.items[4].tax_rate_pct == Decimal("28.00")


# =========================================================================
# Scenario 17: Missing Supplier Total -> CALCULATED_ONLY
# =========================================================================
def test_scenario_17_missing_supplier_total_sets_calculated_only(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Supplier: Delta Corp", "Quote: DEL-10"])
        ws.append(["Description", "Qty", "Rate", "Tax %"])
        ws.append(["Hex Bolt M8x30", 50, 6.0, 18])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    reconciler = QuoteReconciler()
    report = reconciler.reconcile(quote, stated_grand_total=None)
    assert report.financial_model.reconciliation_status == ReconciliationStatus.CALCULATED_ONLY
    assert report.financial_model.discrepancy is None
    assert report.overall_status == ValidationStatus.CALCULATED_ONLY


# =========================================================================
# Scenario 18: Incorrect Supplier Total -> DISCREPANCY
# =========================================================================
def test_scenario_18_incorrect_supplier_total_sets_reconciliation_failed(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Supplier: Delta Corp", "Quote: DEL-10"])
        ws.append(["Description", "Qty", "Rate", "Tax %"])
        ws.append(["Hex Bolt M8x30", 50, 6.0, 18])  # Landed = 354.00

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    reconciler = QuoteReconciler()
    # Supplier stated total 999.00 vs calculated 354.00
    report = reconciler.reconcile(quote, stated_grand_total=Decimal("999.00"))
    assert report.financial_model.reconciliation_status == ReconciliationStatus.DISCREPANCY
    assert report.overall_status == ValidationStatus.DISCREPANCY
    assert report.calibrated_confidence <= 0.60



# =========================================================================
# Scenario 19: Duplicate Items / Continuation Rows
# =========================================================================
def test_scenario_19_continuation_rows(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "Rate", "GST %"])
        ws.append(["Electric Motor 3 Phase 5HP", 1, 14500.0, 18])
        ws.append(["Class F Insulation, 1440 RPM, Foot Mounted", "", "", ""])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    assert "1440 RPM" in quote.items[0].raw_description


# =========================================================================
# Scenario 20: Table Starting at Arbitrary Row/Column (Row 15, Column D)
# =========================================================================
def test_scenario_20_table_starting_at_arbitrary_position(tmp_path):
    def build(wb):
        ws = wb.active
        # Populate table at Row 15, starting at Col 4 (D)
        ws.cell(row=15, column=4, value="Item Description")
        ws.cell(row=15, column=5, value="Qty")
        ws.cell(row=15, column=6, value="Rate")
        ws.cell(row=15, column=7, value="GST %")

        ws.cell(row=16, column=4, value="Stainless Steel Pipe 2 inch")
        ws.cell(row=16, column=5, value=12)
        ws.cell(row=16, column=6, value=650.0)
        ws.cell(row=16, column=7, value=18)

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    assert quote.items[0].raw_description == "Stainless Steel Pipe 2 inch"
    assert quote.items[0].unit_price == Decimal("650.00")


# =========================================================================
# Scenario 21: Irrelevant Content Outside Quote Table (Must NOT become items)
# =========================================================================
def test_scenario_21_irrelevant_content_outside_table(tmp_path):
    def build(wb):
        ws = wb.active
        ws.append(["Random Note: Please visit our warehouse in Mumbai."])
        ws.append(["Office Timing: 9 AM to 6 PM"])
        ws.append([])
        ws.append(["Description", "Qty", "Rate", "GST %"])
        ws.append(["Drill Bit 8mm HSS", 30, 45.0, 18])
        ws.append([])
        ws.append(["Bank Account: HDFC Bank 50200012345678 IFSC: HDFC0001234"])
        ws.append(["Branch: Andheri East Mumbai"])

    quote = parse_and_extract_bytes(create_workbook_bytes(build), tmp_path)
    assert len(quote.items) == 1
    assert quote.items[0].raw_description == "Drill Bit 8mm HSS"
    assert not any("bank" in it.raw_description.lower() for it in quote.items)
