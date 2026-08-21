from datetime import date
from decimal import Decimal
from pathlib import Path
import pytest

from core.canonical_quote import CanonicalQuote, ChargeType
from extraction.document_profiler import DocumentProfiler
from extraction.extractor import QuoteExtractor
from extraction.financial_engine import FinancialEngine
from extraction.table_classifier import TableClassifier, TableType
from parsers.excel_parser import ExcelParser


@pytest.fixture
def adversarial_file_path():
    base_dir = Path(__file__).resolve().parent.parent
    file_path = base_dir / "test_runs" / "TEST-0011" / "source" / "adversarial_quote_benchmark.xlsx"
    if not file_path.exists():
        for p in base_dir.glob("**/adversarial_quote_benchmark.xlsx"):
            return p
        pytest.skip("adversarial_quote_benchmark.xlsx not found")
    return file_path


def test_document_profiler_and_table_classifier(adversarial_file_path):
    """Verifies that DocumentProfiler and TableClassifier correctly classify workbook topology."""
    parser = ExcelParser()
    ast = parser.parse(adversarial_file_path)
    
    profile = DocumentProfiler.profile_ast(ast)
    assert profile.has_multiple_sheets is True
    assert len(profile.sheets) >= 6

    classifier = TableClassifier()
    
    # Check that volume tier table is classified as TableType.VOLUME_PRICING
    tier_tables = [t for t in ast.tables if t.sheet_name == "Quotation" and "Min Qty" in t.headers]
    assert len(tier_tables) >= 1
    t_type, conf = classifier.classify_table(tier_tables[0])
    assert t_type == TableType.VOLUME_PRICING
    assert conf >= 0.90

    # Check that main item table is classified as TableType.MAIN_LINE_ITEMS
    main_tables = [t for t in ast.tables if t.sheet_name == "Quotation" and "Item Code" in t.headers]
    assert len(main_tables) >= 1
    m_type, m_conf = classifier.classify_table(main_tables[0])
    assert m_type == TableType.MAIN_LINE_ITEMS
    assert m_conf >= 0.90


def test_false_positive_item_rate_is_zero(adversarial_file_path):
    """
    Verifies that the False Positive Item Rate is exactly 0%:
    - Zero items from Volume Pricing tier table
    - Zero items from Subtotal/Grand Total/Freight/Packing rows
    - Zero items from Notes
    - Exactly 4 genuine QuoteItems extracted
    """
    parser = ExcelParser()
    ast = parser.parse(adversarial_file_path)
    quote = QuoteExtractor().extract(ast)

    assert len(quote.items) == 4, f"False Positive Items detected! Expected 4, got {len(quote.items)}"
    
    # Check that no item description contains volume tier or subtotal keywords
    for item in quote.items:
        desc_lower = item.raw_description.lower()
        assert "min qty" not in desc_lower
        assert "max qty" not in desc_lower
        assert "unit price" not in desc_lower
        assert "subtotal" not in desc_lower
        assert "grand total" not in desc_lower
        assert "freight" not in desc_lower
        assert "packing" not in desc_lower


def test_financial_engine_pure_decimal_arithmetic():
    """Verifies that the unified financial engine computes exact landed costs with discounts and tiers."""
    engine = FinancialEngine()
    
    # Test 1: Bearing (Qty 20, Rate 450, Disc 5%, Tax 18%)
    eff_price, net, tax, landed = engine.calculate_line_landed_cost(
        qty=Decimal("20"),
        unit_price=Decimal("450"),
        discount_pct=Decimal("5"),
        tax_rate_pct=Decimal("18")
    )
    assert eff_price == Decimal("450")
    assert net == Decimal("8550.0000")
    assert tax == Decimal("1539.0000")
    assert landed == Decimal("10089.00")

    # Test 2: Tape (Qty 50, Rate 38, Disc 10%, Tax 18%)
    eff_price, net, tax, landed = engine.calculate_line_landed_cost(
        qty=Decimal("50"),
        unit_price=Decimal("38"),
        discount_pct=Decimal("10"),
        tax_rate_pct=Decimal("18")
    )
    assert eff_price == Decimal("38")
    assert net == Decimal("1710.0000")
    assert tax == Decimal("307.8000")
    assert landed == Decimal("2017.80")
