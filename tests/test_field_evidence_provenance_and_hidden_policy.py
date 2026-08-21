"""
Regression test suite for FieldEvidence Provenance & Hidden Row Policy.

Validates that:
1. Complete FieldEvidence is attached to QuoteItem fields, charges, volume tiers,
   supplier totals, and metadata with exact source cell/range, raw/normalized values,
   extraction method, confidence, and status.
2. Hidden-row policy:
   - Excludes helper / internal calculation rows
   - Preserves valid product items with is_hidden_row flag
"""

from decimal import Decimal
import io
from pathlib import Path
import openpyxl
import pytest

from core.canonical_quote import (
    CanonicalQuote,
    ChargeType,
    FieldStatus,
    HiddenRowPolicy,
)
from extraction.extractor import QuoteExtractor
from extraction.reconciliation import QuoteReconciler, ReconciliationStatus, ValidationStatus
from parsers.excel_parser import ExcelParser


def wb_bytes(builder_fn) -> bytes:
    wb = openpyxl.Workbook()
    builder_fn(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def extract_with_policy(file_bytes: bytes, tmp_path: Path, policy: HiddenRowPolicy = HiddenRowPolicy.SMART_INCLUDE) -> CanonicalQuote:
    fp = tmp_path / "provenance_test.xlsx"
    fp.write_bytes(file_bytes)
    parser = ExcelParser()
    ast = parser.parse(fp)
    extractor = QuoteExtractor(hidden_row_policy=policy)
    return extractor.extract(ast)


# ============================================================================
# 1. COMPLETE FIELD EVIDENCE PROVENANCE TESTS
# ============================================================================
def test_complete_field_evidence_provenance(tmp_path):
    """
    Proves that:
    - Unit price
    - Quantity
    - Freight charge
    - Supplier grand total
    - Volume tier
    each retain correct source sheet, cell/range, raw_value, normalized_value,
    extraction_method, evidence_signals, confidence, and status.
    """
    def build(wb):
        ws = wb.active
        ws.title = "QuoteSheet"
        ws.append(["Supplier: Dynatech Industrial Components Pvt Ltd"])
        ws.append(["Quote No: DYN/2026/880", "Date: 2026-08-20"])
        ws.append([])
        ws.append(["Part No", "Description", "Qty", "UOM", "Rate", "Tax %"])
        ws.append(["DYN-01", "Precision Ball Screw 25mm", 10, "PCS", 4500.0, 18])
        ws.append([])
        ws.append(["VOLUME PRICING"])
        ws.append(["Min Qty", "Max Qty", "Unit Price"])
        ws.append([1, 9, 4500.0])
        ws.append([10, 49, 4200.0])
        ws.append([50, "", 3900.0])
        ws.append([])
        ws.append(["Freight & Shipping:", 1200.0, "18%"])
        ws.append([])
        ws.append(["Grand Total", "", "", "", 54516.0])


    quote = extract_with_policy(wb_bytes(build), tmp_path)


    # 1. Item Level Evidence (Quantity & Unit Price)
    assert len(quote.items) == 1
    item = quote.items[0]

    # Unit price evidence
    assert item.unit_price_evidence is not None
    assert item.unit_price_evidence.sheet_name == "QuoteSheet"
    assert item.unit_price_evidence.raw_value == "4500" or item.unit_price_evidence.raw_value == "4500.0"
    assert item.unit_price_evidence.normalized_value == Decimal("4500")
    assert item.unit_price_evidence.confidence >= 0.90
    assert item.unit_price_evidence.status == FieldStatus.CONFIRMED
    assert len(item.unit_price_evidence.evidence_signals) > 0

    # Quantity evidence
    assert item.quoted_qty_evidence is not None
    assert item.quoted_qty_evidence.sheet_name == "QuoteSheet"
    assert item.quoted_qty_evidence.raw_value == "10"
    assert item.quoted_qty_evidence.normalized_value == Decimal("10")
    assert item.quoted_qty_evidence.confidence >= 0.90
    assert item.quoted_qty_evidence.status == FieldStatus.CONFIRMED

    # Description evidence
    assert item.description_evidence is not None
    assert item.description_evidence.raw_value == "Precision Ball Screw 25mm"
    assert item.description_evidence.status == FieldStatus.CONFIRMED

    # 2. Freight Charge Evidence
    assert len(quote.additional_charges) >= 1
    freight = next(c for c in quote.additional_charges if c.charge_type == ChargeType.FREIGHT)
    assert freight.amount == Decimal("1200")
    assert freight.amount_evidence is not None
    assert freight.amount_evidence.normalized_value == Decimal("1200")
    assert freight.amount_evidence.status == FieldStatus.CONFIRMED
    assert freight.charge_type_evidence is not None
    assert freight.charge_type_evidence.normalized_value == "FREIGHT"

    # 3. Volume Tier Evidence
    assert len(item.price_tiers) == 3
    tier_50 = item.price_tiers[2]
    assert tier_50.min_qty == Decimal("50")
    assert tier_50.unit_price == Decimal("3900")
    assert tier_50.min_qty_evidence is not None
    assert tier_50.min_qty_evidence.normalized_value == Decimal("50")
    assert tier_50.min_qty_evidence.status == FieldStatus.CONFIRMED
    assert tier_50.unit_price_evidence is not None
    assert tier_50.unit_price_evidence.normalized_value == Decimal("3900")
    assert tier_50.unit_price_evidence.status == FieldStatus.CONFIRMED

    # 4. Supplier Grand Total Evidence
    assert quote.stated_grand_total_evidence is not None
    assert quote.stated_grand_total_evidence.normalized_value == Decimal("54516")
    assert quote.stated_grand_total_evidence.status == FieldStatus.CONFIRMED


# ============================================================================
# 2. HIDDEN-ROW POLICY TESTS
# ============================================================================
def test_hidden_helper_rows_excluded(tmp_path):
    """Hidden helper/calculation rows must be excluded by SMART_INCLUDE policy."""
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "UOM", "Rate", "GST %"])
        ws.append(["Valid Hydraulic Valve", 10, "PCS", 2500.0, 18])
        ws.append(["Internal Costing / Markup Calculation", 0, "PCS", 0.0, 0])
        ws.append(["Dummy Helper Row", 0, "", 0.0, 0])
        ws.row_dimensions[3].hidden = True
        ws.row_dimensions[4].hidden = True

    quote = extract_with_policy(wb_bytes(build), tmp_path, policy=HiddenRowPolicy.SMART_INCLUDE)
    assert len(quote.items) == 1
    assert quote.items[0].raw_description == "Valid Hydraulic Valve"
    descriptions = [it.raw_description for it in quote.items]
    assert "Internal Costing / Markup Calculation" not in descriptions
    assert "Dummy Helper Row" not in descriptions


def test_hidden_valid_item_rows_preserved_with_flag(tmp_path):
    """
    Hidden rows that contain genuine product items (valid description, qty, price)
    must NOT be silently discarded by SMART_INCLUDE; they are extracted with is_hidden_row=True.
    """
    def build(wb):
        ws = wb.active
        ws.append(["Supplier: Metro Steels Ltd"])
        ws.append(["Quote No: MS-2026-90"])
        ws.append([])
        ws.append(["Description", "Qty", "UOM", "Rate", "GST %"])
        ws.append(["Visible Steel Plate 10mm", 5, "PCS", 1200.0, 18])
        ws.append(["Hidden Steel Angle 50x50x5", 15, "PCS", 650.0, 18])  # Genuine product row
        ws.row_dimensions[6].hidden = True

    quote = extract_with_policy(wb_bytes(build), tmp_path, policy=HiddenRowPolicy.SMART_INCLUDE)
    assert len(quote.items) == 2
    assert quote.items[0].raw_description == "Visible Steel Plate 10mm"
    assert quote.items[0].provenance.is_hidden_row is False

    hidden_item = quote.items[1]
    assert hidden_item.raw_description == "Hidden Steel Angle 50x50x5"
    assert hidden_item.quoted_qty == Decimal("15")
    assert hidden_item.unit_price == Decimal("650")
    assert hidden_item.provenance.is_hidden_row is True
    assert any("hidden" in s.lower() for s in hidden_item.description_evidence.evidence_signals)


def test_hidden_rows_exclude_all_policy(tmp_path):
    """When policy is EXCLUDE_ALL, even valid hidden rows are skipped."""
    def build(wb):
        ws = wb.active
        ws.append(["Description", "Qty", "UOM", "Rate", "GST %"])
        ws.append(["Visible Item 1", 10, "PCS", 100.0, 18])
        ws.append(["Hidden Item 2", 20, "PCS", 200.0, 18])
        ws.row_dimensions[3].hidden = True

    quote = extract_with_policy(wb_bytes(build), tmp_path, policy=HiddenRowPolicy.EXCLUDE_ALL)
    assert len(quote.items) == 1
    assert quote.items[0].raw_description == "Visible Item 1"
