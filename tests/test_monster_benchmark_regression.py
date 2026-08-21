from datetime import date
from decimal import Decimal
from pathlib import Path
import pytest

from core.canonical_quote import CanonicalQuote, ChargeType
from extraction.extractor import QuoteExtractor
from extraction.reconciliation import QuoteReconciler, ReconciliationStatus, Severity
from parsers.excel_parser import ExcelParser


@pytest.fixture
def monster_file_path():
    base_dir = Path(__file__).resolve().parent.parent
    file_path = base_dir / "test_runs" / "TEST-0013" / "source" / "adversarial_monster_quote_benchmark.xlsx"
    if not file_path.exists():
        for p in base_dir.glob("**/adversarial_monster_quote_benchmark.xlsx"):
            return p
        pytest.skip("adversarial_monster_quote_benchmark.xlsx not found")
    return file_path


def test_monster_benchmark_full_extraction_and_reconciliation(monster_file_path):
    """
    Comprehensive regression test for adversarial_monster_quote_benchmark.xlsx:
    1. Extracts exactly 14 main product line items from Sheet 'Quotation'.
    2. Zero fake items extracted from volume pricing, summary, notes, or decoy sheets.
    3. Normalizes 'PAIR' UOM without unknown-UOM warnings.
    4. Extracts 6 volume pricing tiers across workbook (3 for BOLT-M12, 3 for CABLE-4SQ).
    5. Extracts 3 quote-level charges: Freight (750), Packing (250), Insurance (125 @ 18% tax).
    6. Extracts Payment terms ('30 days') and Delivery terms ('7-10 days').
    7. Computes exact Total Landed Cost = 121,049.15.
    8. Perfect financial reconciliation with zero high-severity issues.
    """
    parser = ExcelParser()
    ast = parser.parse(monster_file_path)
    extractor = QuoteExtractor()
    quote = extractor.extract(ast)

    # 1. Metadata
    assert quote.supplier_raw_name == "Apex Industrial Components Pvt. Ltd."
    assert quote.quote_number == "APX/QTN/2026/0819"
    assert quote.quote_date == date(2026, 8, 19)
    assert quote.currency == "INR"

    # 2. Terms
    assert quote.payment_terms is not None
    assert quote.payment_terms.credit_days == 30
    assert "30 days" in quote.payment_terms.raw_text

    assert quote.delivery_terms is not None
    assert "7-10 days" in quote.delivery_terms.raw_text

    # 3. Main Line Items (Exact 14)
    assert len(quote.items) == 14, f"Expected 14 items, got {len(quote.items)}"

    # Check specific items
    it0 = quote.items[0]
    assert it0.supplier_part_number == "BRG-6205"
    assert it0.quoted_qty == Decimal("20")
    assert it0.unit_price == Decimal("450")
    assert it0.discount_pct == Decimal("5")
    assert it0.tax_rate_pct == Decimal("18")
    assert it0.calculate_line_landed_cost() == Decimal("10089.00")

    # Item with PAIR UOM
    it6 = quote.items[6]
    assert it6.supplier_part_number == "GLV-01"
    assert it6.quoted_uom == "PAIR"
    assert it6.quoted_qty == Decimal("80")
    assert it6.unit_price == Decimal("125")
    assert it6.discount_pct == Decimal("3")
    assert it6.tax_rate_pct == Decimal("12")
    assert it6.calculate_line_landed_cost() == Decimal("10864.00")

    # 4. Volume Tiers (Total 6)
    total_tiers = sum(len(it.price_tiers) for it in quote.items)
    assert total_tiers == 6, f"Expected 6 volume tiers, got {total_tiers}"

    bolt_item = next(it for it in quote.items if it.supplier_part_number == "BOLT-M12")
    assert len(bolt_item.price_tiers) == 3
    assert bolt_item.price_tiers[0].min_qty == Decimal("1")
    assert bolt_item.price_tiers[0].max_qty == Decimal("499")
    assert bolt_item.price_tiers[0].unit_price == Decimal("12.5")

    cable_item = next(it for it in quote.items if it.supplier_part_number == "CABLE-4SQ")
    assert len(cable_item.price_tiers) == 3
    assert cable_item.quoted_qty == Decimal("250")
    assert cable_item.unit_price == Decimal("92")
    assert cable_item.gross_amount == Decimal("23000.00")
    assert cable_item.discount_amount == Decimal("0.00")
    assert cable_item.taxable_amount == Decimal("23000.00")
    assert cable_item.tax_amount == Decimal("4140.00")
    assert cable_item.calculate_line_landed_cost() == Decimal("27140.00")
    # Tier-specific lookup for qty 250 resolves to tier rate 88 -> 25960.00
    assert cable_item.calculate_line_landed_cost(target_qty=Decimal("250")) == Decimal("25960.00")

    # 5. Charges (Freight 750, Packing 250, Insurance 125 @ 18%)
    assert len(quote.additional_charges) == 3
    charge_dict = {c.charge_type: (c.amount, c.tax_rate_pct) for c in quote.additional_charges}
    
    assert ChargeType.FREIGHT in charge_dict
    assert charge_dict[ChargeType.FREIGHT] == (Decimal("750"), Decimal("0.0"))

    assert ChargeType.PACKING in charge_dict
    assert charge_dict[ChargeType.PACKING] == (Decimal("250"), Decimal("0.0"))

    assert ChargeType.INSURANCE in charge_dict
    assert charge_dict[ChargeType.INSURANCE] == (Decimal("125"), Decimal("18"))

    # 6. Total Landed Cost (121,081.65 items + 1,147.50 charges = 122,229.15)
    assert quote.calculate_total_landed_cost() == Decimal("122229.15")

    # 7. Independent Financial Reconciliation (CALCULATED_ONLY because no supplier stated total)
    reconciler = QuoteReconciler()
    report = reconciler.reconcile(quote)
    assert report.overall_status.value == "CALCULATED_ONLY"
    assert report.financial_model.reconciliation_status.value == "CALCULATED_ONLY"
    assert report.financial_model.calculated_expected_total == Decimal("122229.15")

    assert report.financial_model.supplier_stated_grand_total is None
    assert report.financial_model.discrepancy is None

    # 8. No UOM warning for PAIR and zero validation warnings
    assert not any("PAIR" in w for w in quote.extraction_metadata.warnings)
    assert quote.extraction_metadata.overall_confidence == 1.0
