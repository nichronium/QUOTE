from decimal import Decimal
import json
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import services
from core.currency import CurrencyRateService, LiveRateResult
from matching.models import RFQLineItem, MatchedQuoteItem, MatchCandidate, MatchStatus, MatchMethod
from core.canonical_quote import CanonicalQuote, QuoteItem, ExtractionMetadata


@pytest.fixture
def client():
    return TestClient(app)


def test_currency_rate_service():
    """Verifies live rate fetching, caching, Decimal precision, and provider metadata."""
    # 1. Base to same base
    res_same = CurrencyRateService.get_exchange_rate("INR", "INR")
    assert res_same.rate == Decimal("1.0")
    assert res_same.is_live is True
    assert res_same.status == "BASE_CURRENCY"

    # 2. Live rate (USD to INR)
    res_usd = CurrencyRateService.get_exchange_rate("USD", "INR", force_refresh=True)
    assert isinstance(res_usd.rate, Decimal)
    assert res_usd.rate > Decimal("50.0")
    assert res_usd.base == "USD"
    assert res_usd.quote == "INR"
    assert "European Central Bank" in res_usd.provider or res_usd.is_fallback
    assert res_usd.is_live is True or res_usd.is_fallback is True

    # 3. Cache verification
    res_usd_cached = CurrencyRateService.get_exchange_rate("USD", "INR")
    assert res_usd_cached.rate == res_usd.rate
    assert res_usd_cached.status == "CACHED"
    assert res_usd_cached.is_cached is True
    assert res_usd_cached.timestamp == res_usd.timestamp


def test_partial_supplier_participates_in_matrix_and_split_sourcing(client: TestClient):
    """
    Verifies that a partial supplier (6/8 items) appears in the item comparison matrix,
    missing items show 'Not Quoted', and wins individual line items in split sourcing.
    """
    # 1. Get 8 items from the real Item Master
    catalog = services.get_item_master()
    assert len(catalog) >= 8
    target_catalog_items = catalog[:8]

    rfq_id = "RFQ-TEST-8ITEMS-REAL-001"
    rfq_items = [
        {
            "rfq_line_id": f"LINE-0{idx+1}",
            "sku": item.internal_sku,
            "description": item.canonical_description,
            "requested_quantity": 10,
            "requested_uom": item.stocking_uom
        }
        for idx, item in enumerate(target_catalog_items)
    ]
    services.create_rfq(rfq_id, "8-Item Real Scope RFQ", "INR", rfq_items)

    # 2. Ingest Complete Supplier (Quotes all 8 items at ₹100 each)
    q1_id = services.create_quote("Alpha Complete", rfq_id)
    quote1 = CanonicalQuote(
        quote_id=q1_id,
        supplier_raw_name="Alpha Complete",
        currency="INR",
        items=[
            QuoteItem(
                line_index=idx+1,
                supplier_part_number=item.internal_sku,
                raw_description=item.canonical_description,
                quoted_qty=Decimal("10"),
                quoted_uom=item.stocking_uom,
                unit_price=Decimal("100.00"),
                net_unit_price=Decimal("100.00")
            )
            for idx, item in enumerate(target_catalog_items)
        ],
        extraction_metadata=ExtractionMetadata(source_file_name="alpha.xlsx", source_file_hash="hash1", parser_used="TEST", overall_confidence=1.0)
    )
    # Save canonical quote
    q1_dir = services.DATA_DIR / q1_id / "extracted"
    q1_dir.mkdir(parents=True, exist_ok=True)
    with open(q1_dir / "canonical_quote.json", "w", encoding="utf-8") as f:
        f.write(quote1.model_dump_json(indent=2))
    meta1 = services.get_quote_metadata(q1_id) or {}
    meta1["status"] = "SUCCESS"
    with open(services.DATA_DIR / q1_id / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta1, f, indent=2)

    # 3. Ingest Partial Supplier (Quotes only 6 items at ₹50 each for items 1-6, missing 7 and 8)
    q2_id = services.create_quote("Beta Partial", rfq_id)
    quote2 = CanonicalQuote(
        quote_id=q2_id,
        supplier_raw_name="Beta Partial",
        currency="INR",
        items=[
            QuoteItem(
                line_index=idx+1,
                supplier_part_number=item.internal_sku,
                raw_description=item.canonical_description,
                quoted_qty=Decimal("10"),
                quoted_uom=item.stocking_uom,
                unit_price=Decimal("50.00"),
                net_unit_price=Decimal("50.00")
            )
            for idx, item in enumerate(target_catalog_items[:6])
        ],
        extraction_metadata=ExtractionMetadata(source_file_name="beta.xlsx", source_file_hash="hash2", parser_used="TEST", overall_confidence=1.0)
    )
    q2_dir = services.DATA_DIR / q2_id / "extracted"
    q2_dir.mkdir(parents=True, exist_ok=True)
    with open(q2_dir / "canonical_quote.json", "w", encoding="utf-8") as f:
        f.write(quote2.model_dump_json(indent=2))
    meta2 = services.get_quote_metadata(q2_id) or {}
    meta2["status"] = "SUCCESS"
    with open(services.DATA_DIR / q2_id / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta2, f, indent=2)

    # 4. Run matching for both
    services.run_matching_for_quote(q1_id, rfq_id)
    services.run_matching_for_quote(q2_id, rfq_id)

    # 5. Check get_quotes_for_rfq coverage classification
    quotes = services.get_quotes_for_rfq(rfq_id)
    q1_entry = next(q for q in quotes if q["test_id"] == q1_id)
    q2_entry = next(q for q in quotes if q["test_id"] == q2_id)

    assert q1_entry["coverage_state"] == "COMPLETE"
    assert q1_entry["whole_rfq_status"] in ["Eligible (Whole-RFQ)", "Complete (100% Scope)"]
    assert q1_entry["is_eligible"] is True

    assert q2_entry["coverage_state"] == "PARTIAL"
    assert q2_entry["whole_rfq_status"] in ["Partial (Item-Level Only)", "Partial Scope"]
    assert q2_entry["is_eligible"] is True  # Eligible to participate in comparison

    # 6. Execute RFQ Comparison
    comp_dict = services.run_rfq_comparison(rfq_id, [q1_id, q2_id], base_currency="INR")
    comp_id = comp_dict["comparison_id"]

    ranking_rep = comp_dict["ranking_report"]
    # Alpha Complete is Whole-RFQ L1
    assert ranking_rep["l1_supplier"]["supplier_name"] == "Alpha Complete"
    assert ranking_rep["l1_supplier"]["is_eligible"] is True

    # Beta Partial is in item split recommendations for lines 1-6 (since ₹50 < ₹100)
    splits = ranking_rep["item_split_recommendations"]
    assert splits["LINE-01"]["l1_supplier_name"] == "Beta Partial"
    assert splits["LINE-02"]["l1_supplier_name"] == "Beta Partial"
    assert splits["LINE-07"]["l1_supplier_name"] == "Alpha Complete"
    assert splits["LINE-08"]["l1_supplier_name"] == "Alpha Complete"

    # 7. Render Comparison Detail Page
    res_page = client.get(f"/comparisons/{comp_id}")
    assert res_page.status_code == 200
    assert "Participating Suppliers &amp; Coverage Audit" in res_page.text
    assert "Partial (Item-Level Only)" in res_page.text
    assert "Alpha Complete" in res_page.text
    assert "Beta Partial" in res_page.text
    assert "Not Quoted" in res_page.text  # Missing lines for Beta Partial
