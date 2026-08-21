from decimal import Decimal
from pathlib import Path
import pytest

from core.canonical_quote import (
    AdditionalCharge,
    CanonicalQuote,
    ChargeType,
    ExtractionMetadata,
    ProcessingMode,
    QuoteItem,
)
from extraction.commercial_charges import CommercialChargeExtractor
from extraction.extractor import QuoteExtractor
from extraction.normalizer import normalize_decimal, normalize_uom
from extraction.reconciliation import (
    QuoteReconciler,
    ReconciliationStatus,
    Severity,
    ValidationStatus,
)
from extraction.semantic_registry import STANDARD_UOM_SET


def test_charge_token_isolation_prevents_concatenation():
    """Verifies that tab/whitespace-separated numbers are NEVER glued into corrupted amounts."""
    assert normalize_decimal("Freight\t750\t0") == Decimal("750")
    assert normalize_decimal("Packing\t250\t0") == Decimal("250")
    assert normalize_decimal("Insurance\t125\t18") == Decimal("125")
    
    extractor = CommercialChargeExtractor()
    charges = extractor._extract_from_text("Insurance\t125\t18", 1, "Terms_Charges")
    assert len(charges) == 1
    assert charges[0].charge_type == ChargeType.INSURANCE
    assert charges[0].amount == Decimal("125")
    assert charges[0].tax_rate_pct == Decimal("18")
    assert charges[0].total_with_tax == Decimal("147.50")


def test_uom_pair_recognition_and_standard_registry():
    """Verifies that PAIR, PAIRS, and PR normalize to PAIR with zero warnings."""
    assert normalize_uom("pair") == "PAIR"
    assert normalize_uom("Pairs") == "PAIR"
    assert normalize_uom("PR") == "PAIR"
    assert "PAIR" in STANDARD_UOM_SET

    item = QuoteItem(
        line_index=0,
        raw_description="Safety Gloves",
        quoted_qty=Decimal("10"),
        quoted_uom="PAIR",
        unit_price=Decimal("100"),
        tax_rate_pct=Decimal("18")
    )
    quote = CanonicalQuote(
        quote_id="Q-TEST",
        supplier_raw_name="ABC Safety Corp",
        currency="INR",
        items=[item],
        extraction_metadata=ExtractionMetadata(
            source_file_name="test.xlsx",
            source_file_hash="123",
            parser_used="test",
            overall_confidence=1.0,
            warnings=[],
            processing_mode=ProcessingMode.LOCAL
        )
    )
    reconciler = QuoteReconciler()
    report = reconciler.reconcile(quote)
    assert not any("PAIR" in w for w in report.warning_messages)
    assert report.is_valid is True


def test_anti_circularity_financial_reconciliation(monkeypatch):
    """
    MANDATORY ANTI-CIRCULARITY TEST:
    Modifying CanonicalQuote total calculation or mutating quote methods must NOT affect
    the independent reconstruction of expected total.
    """
    item1 = QuoteItem(
        line_index=0,
        raw_description="Hex Bolt",
        quoted_qty=Decimal("100"),
        quoted_uom="PCS",
        unit_price=Decimal("10.0"),
        discount_pct=Decimal("0.0"),
        tax_rate_pct=Decimal("18.0")
    ) # Net: 1000, Tax: 180, Landed: 1180

    charge1 = AdditionalCharge(
        charge_type=ChargeType.FREIGHT,
        amount=Decimal("500"),
        tax_rate_pct=Decimal("0.0")
    ) # Total: 500

    quote = CanonicalQuote(
        quote_id="Q-TEST",
        supplier_raw_name="ABC Fasteners Ltd",
        quote_number="REF-100",
        currency="INR",
        items=[item1],
        additional_charges=[charge1],
        extraction_metadata=ExtractionMetadata(
            source_file_name="test.xlsx",
            source_file_hash="123",
            parser_used="test",
            overall_confidence=1.0,
            warnings=[],
            processing_mode=ProcessingMode.LOCAL
        )
    )

    reconciler = QuoteReconciler()

    # 1. Independent Expected Total = 1180 + 500 = 1680.00
    report1 = reconciler.reconcile(quote, stated_grand_total=Decimal("1680.00"))
    assert report1.financial_model.calculated_expected_total == Decimal("1680.00")
    assert report1.financial_model.reconciliation_status == ReconciliationStatus.RECONCILED
    assert report1.overall_status == ValidationStatus.RECONCILED

    # 2. Mutate CanonicalQuote total calculation method on the class
    monkeypatch.setattr(CanonicalQuote, "calculate_total_landed_cost", lambda self: Decimal("999999.99"))
    assert quote.calculate_total_landed_cost() == Decimal("999999.99")

    # Reconciliation must STILL independently calculate 1680.00
    report2 = reconciler.reconcile(quote, stated_grand_total=Decimal("1680.00"))
    assert report2.financial_model.calculated_expected_total == Decimal("1680.00")
    assert report2.financial_model.reconciliation_status == ReconciliationStatus.RECONCILED
    assert report2.overall_status == ValidationStatus.RECONCILED


def test_financial_discrepancy_reduces_confidence_and_fails_validation():
    """Verifies that an unreconciled total sets status to DISCREPANCY and caps confidence."""
    item = QuoteItem(
        line_index=0,
        raw_description="Bearing",
        quoted_qty=Decimal("10"),
        quoted_uom="PCS",
        unit_price=Decimal("100.0"),
        tax_rate_pct=Decimal("18.0")
    ) # Landed = 1180.00

    quote = CanonicalQuote(
        quote_id="Q-TEST",
        supplier_raw_name="Apex Bearings",
        quote_number="APX-01",
        currency="INR",
        items=[item],
        extraction_metadata=ExtractionMetadata(

            source_file_name="test.xlsx",
            source_file_hash="123",
            parser_used="test",
            overall_confidence=1.0,
            warnings=[],
            processing_mode=ProcessingMode.LOCAL
        )
    )

    reconciler = QuoteReconciler()
    # Supplier stated total is 2000.00, but calculated expected total is 1180.00
    report = reconciler.reconcile(quote, stated_grand_total=Decimal("2000.00"))

    assert report.financial_model.calculated_expected_total == Decimal("1180.00")
    assert report.financial_model.supplier_stated_grand_total == Decimal("2000.00")
    assert report.financial_model.discrepancy == Decimal("820.00")
    assert report.financial_model.reconciliation_status == ReconciliationStatus.DISCREPANCY
    assert report.overall_status == ValidationStatus.DISCREPANCY
    assert report.is_valid is False
    assert report.calibrated_confidence <= 0.60


def test_line_item_arithmetic_invariant_and_financial_breakdown():
    """
    INVARIANT:
    IF discount = 0, tax = 18%, qty = 250, unit_price = 92
    THEN gross = 23000, taxable = 23000, tax = 4140, landed_total = 27140
    """
    item = QuoteItem(
        line_index=0,
        raw_description="Copper Cable 4 sq mm FR PVC",
        quoted_qty=Decimal("250"),
        quoted_uom="MTR",
        unit_price=Decimal("92"),
        discount_pct=Decimal("0"),
        tax_rate_pct=Decimal("18")
    )
    assert item.gross_amount == Decimal("23000.00")
    assert item.discount_amount == Decimal("0.00")
    assert item.taxable_amount == Decimal("23000.00")
    assert item.tax_amount == Decimal("4140.00")
    assert item.calculate_line_landed_cost() == Decimal("27140.00")


def test_unreconciled_supplier_total_sets_calculated_only_status():
    """
    When supplier-stated total is NOT FOUND in source:
    - reconciliation_status must be CALCULATED_ONLY
    - discrepancy must be None (not 0.00)
    - overall_status must be CALCULATED_ONLY
    """
    item = QuoteItem(
        line_index=0,
        raw_description="Hex Bolt",
        quoted_qty=Decimal("100"),
        quoted_uom="PCS",
        unit_price=Decimal("10.0"),
        discount_pct=Decimal("0.0"),
        tax_rate_pct=Decimal("18.0")
    )
    quote = CanonicalQuote(
        quote_id="Q-TEST",
        supplier_raw_name="Apex Fasteners Ltd",
        quote_number="REF-100",
        currency="INR",
        items=[item],
        extraction_metadata=ExtractionMetadata(
            source_file_name="test.xlsx",
            source_file_hash="123",
            parser_used="test",
            overall_confidence=1.0,
            warnings=[],
            processing_mode=ProcessingMode.LOCAL
        )
    )
    reconciler = QuoteReconciler()
    report = reconciler.reconcile(quote, stated_grand_total=None)
    assert report.financial_model.reconciliation_status == ReconciliationStatus.CALCULATED_ONLY
    assert report.financial_model.discrepancy is None
    assert report.overall_status == ValidationStatus.CALCULATED_ONLY

