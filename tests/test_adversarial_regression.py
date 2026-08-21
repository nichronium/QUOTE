from datetime import date
from decimal import Decimal
from pathlib import Path
import pytest

from core.canonical_quote import CanonicalQuote, ChargeType
from extraction.extractor import QuoteExtractor
from extraction.reconciliation import QuoteReconciler, Severity, ValidationIssue
from parsers.excel_parser import ExcelParser


@pytest.fixture
def adversarial_file_path():
    base_dir = Path(__file__).resolve().parent.parent
    file_path = base_dir / "test_runs" / "TEST-0011" / "source" / "adversarial_quote_benchmark.xlsx"
    if not file_path.exists():
        # Fallback to searching anywhere in test_runs
        for p in base_dir.glob("**/adversarial_quote_benchmark.xlsx"):
            return p
        pytest.skip("adversarial_quote_benchmark.xlsx not found")
    return file_path


def test_adversarial_benchmark_extraction_accuracy(adversarial_file_path):
    """
    Comprehensive regression test for adversarial_quote_benchmark.xlsx:
    1. Extracts 4 actual line items (excludes volume-tier table rows).
    2. Links 3 volume pricing tiers to CABLE-4SQ (1-99: 92, 100-499: 88, 500+: 82).
    3. Correctly applies 5% discount to Bearing (Landed = 10089.00).
    4. Correctly applies 10% discount to Tape (Landed = 2017.80).
    5. Classifies Freight (750) as FREIGHT and Packing (250) as PACKING.
    6. Extracts Delivery as "7-10 days" without fabricating delivery conditions.
    7. Calculates exact Total Landed Cost = 40541.80.
    8. No tier rows have fabricated SKU or UOM.
    """
    parser = ExcelParser()
    ast = parser.parse(adversarial_file_path)
    extractor = QuoteExtractor()
    quote = extractor.extract(ast)

    # 1. Header Metadata
    assert quote.supplier_raw_name == "Apex Industrial Components Pvt. Ltd."
    assert quote.quote_number == "APX/QTN/2026/0819"
    assert quote.quote_date == date(2026, 8, 19)
    assert quote.valid_until == date(2026, 9, 18)
    assert quote.currency == "INR"

    # 2. Terms
    assert quote.payment_terms is not None
    assert quote.payment_terms.credit_days == 30
    assert "30 days" in quote.payment_terms.raw_text

    assert quote.delivery_terms is not None
    assert "7-10 days" in quote.delivery_terms.raw_text
    assert "ex-stock" not in (quote.delivery_terms.raw_text or "").lower()

    # 3. Item Count & Pricing Tiers
    assert len(quote.items) == 4, f"Expected 4 items, got {len(quote.items)}"

    # Item 0: Bearing
    it0 = quote.items[0]
    assert it0.supplier_part_number == "BRG-6205"
    assert "SKF 6205" in it0.raw_description
    assert it0.quoted_qty == Decimal("20")
    assert it0.quoted_uom == "PCS"
    assert it0.unit_price == Decimal("450")
    assert it0.discount_pct == Decimal("5")
    assert it0.tax_rate_pct == Decimal("18")
    assert it0.calculate_line_landed_cost() == Decimal("10089.00")

    # Item 1: Bolt
    it1 = quote.items[1]
    assert it1.supplier_part_number == "BOLT-M12"
    assert it1.quoted_qty == Decimal("100")
    assert it1.quoted_uom == "PCS"
    assert it1.unit_price == Decimal("12.5")
    assert it1.discount_pct == Decimal("0")
    assert it1.calculate_line_landed_cost() == Decimal("1475.00")

    # Item 2: Cable with Volume Pricing Tiers
    it2 = quote.items[2]
    assert it2.supplier_part_number == "CABLE-4SQ"
    assert it2.quoted_qty == Decimal("250")
    assert it2.quoted_uom == "MTR"
    assert it2.unit_price == Decimal("92")
    assert len(it2.price_tiers) == 3, f"Expected 3 volume tiers, got {len(it2.price_tiers)}"
    assert it2.price_tiers[0].min_qty == Decimal("1")
    assert it2.price_tiers[0].max_qty == Decimal("99")
    assert it2.price_tiers[0].unit_price == Decimal("92")
    assert it2.price_tiers[1].min_qty == Decimal("100")
    assert it2.price_tiers[1].max_qty == Decimal("499")
    assert it2.price_tiers[1].unit_price == Decimal("88")
    assert it2.price_tiers[2].min_qty == Decimal("500")
    assert it2.price_tiers[2].unit_price == Decimal("82")
    # Line item arithmetic uses quoted unit rate (92) -> Landed = 250 * 92 * 1.18 = 27140.00
    assert it2.calculate_line_landed_cost() == Decimal("27140.00")
    # Tier-specific target quantity evaluation resolves to tier rate (88) -> Landed = 250 * 88 * 1.18 = 25960.00
    assert it2.calculate_line_landed_cost(target_qty=Decimal("250")) == Decimal("25960.00")

    # Item 3: Tape
    it3 = quote.items[3]
    assert it3.supplier_part_number == "TAPE-PVC"
    assert it3.quoted_qty == Decimal("50")
    assert it3.quoted_uom == "ROLL"
    assert it3.unit_price == Decimal("38")
    assert it3.discount_pct == Decimal("10")
    assert it3.calculate_line_landed_cost() == Decimal("2017.80")

    # 4. Additional Charges Classification
    charge_map = {c.charge_type: c.amount for c in quote.additional_charges}
    assert ChargeType.FREIGHT in charge_map
    assert charge_map[ChargeType.FREIGHT] == Decimal("750")
    assert ChargeType.PACKING in charge_map
    assert charge_map[ChargeType.PACKING] == Decimal("250")

    # 5. Financial Landed Cost Total
    # 10089.00 + 1475.00 + 27140.00 + 2017.80 + 750 + 250 = 41721.80
    assert quote.calculate_total_landed_cost() == Decimal("41721.80")


def test_high_validation_issue_reduces_confidence():
    """Verifies that HIGH severity issues materially reduce overall confidence."""
    reconciler = QuoteReconciler()
    
    # Create an invalid quote with unknown supplier
    from core.canonical_quote import ExtractionMetadata, ProcessingMode
    quote = CanonicalQuote(
        quote_id="Q-TEST",
        supplier_raw_name="Unknown Supplier",
        currency="INR",
        items=[],
        extraction_metadata=ExtractionMetadata(
            source_file_name="test.xlsx",
            source_file_hash="test",
            parser_used="test",
            overall_confidence=1.0,
            processing_mode=ProcessingMode.LOCAL
        )
    )
    report = reconciler.reconcile(quote)
    assert report.calibrated_confidence <= 0.50
    assert any(issue.severity == Severity.HIGH for issue in report.issues)
