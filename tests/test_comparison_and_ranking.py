"""
Comprehensive Multi-Supplier Quotation Comparison & Ranking Test Suite.
Verifies all Stage A comparison rules and Stage B ranking/recommendation invariants.
"""

from decimal import Decimal
import pytest

from core.canonical_quote import (
    AdditionalCharge,
    CanonicalQuote,
    ChargeType,
    DeliveryTerms,
    FieldEvidence,
    FieldStatus,
    PaymentTerms,
    PriceTier,
    Provenance,
    QuoteItem,
    TaxComponent,
)
from matching.models import (
    ItemMasterRecord,
    MatchCandidate,
    MatchedQuoteItem,
    MatchMethod,
    MatchStatus,
    RFQLineItem,
    UOMConversionResult,
)
from comparison.models import (
    ChargeAllocationMethod,
    ComparisonIssueCode,
    ComparisonIssueSeverity,
    RFQDocument,
    SupplierQuoteSubmission,
)
from comparison.comparator import QuotationComparator
from comparison.ranking import RankingEngine


# =========================================================================
# HELPER FACTORIES
# =========================================================================

def make_rfq() -> RFQDocument:
    return RFQDocument(
        rfq_id="RFQ-2026-001",
        title="Industrial Plant Mechanical & Electrical Sourcing",
        base_currency="INR",
        items=[
            RFQLineItem(
                rfq_line_id="RFQ-ITEM-1",
                internal_item_id="ITEM-BEAR-6205",
                sku="BEAR-6205-2RS",
                description="Deep Groove Ball Bearing 25x52x15mm",
                requested_quantity=Decimal("100.0"),
                requested_uom="PCS"
            ),
            RFQLineItem(
                rfq_line_id="RFQ-ITEM-2",
                internal_item_id="ITEM-CABL-6SQ",
                sku="CABL-6SQ-CU",
                description="Copper Armoured Cable 6 Sq.mm",
                requested_quantity=Decimal("500.0"),
                requested_uom="MTR"
            ),
        ]
    )


def make_submission(
    supplier_id: str,
    supplier_name: str,
    items_spec: list,
    currency: str = "INR",
    charges: list = None,
    credit_days: int = None,
    advance_pct: str = None,
    lead_time_days: int = None,
    incoterm: str = "Ex-Works"
) -> SupplierQuoteSubmission:
    quote_items = []
    matched_items = []

    for idx, spec in enumerate(items_spec):
        # spec: (rfq_line_id, desc, price, qty, uom, discount_pct, tax_pct, match_status, tiers)
        rfq_line_id = spec.get("rfq_line_id")
        desc = spec.get("desc", f"Item {idx+1}")
        price = Decimal(str(spec.get("price", "100.0")))
        qty = Decimal(str(spec.get("qty", "100.0")))
        if "uom" in spec:
            uom = spec["uom"]
        else:
            uom = "MTR" if (rfq_line_id == "RFQ-ITEM-2" or "cable" in desc.lower()) else "PCS"


        disc = Decimal(str(spec.get("discount_pct", "0.0")))
        tax = Decimal(str(spec.get("tax_pct", "18.0")))
        status = spec.get("match_status", MatchStatus.EXACT_MATCH)
        tiers = spec.get("tiers", [])

        prov = Provenance(sheet_name="Quotation", row_idx=idx+2, col_idx=0, cell_ref=f"A{idx+3}", source_text=desc)

        q_item = QuoteItem(
            line_index=idx,
            raw_description=desc,
            supplier_part_number=spec.get("sku", f"SKU-{idx+1}"),
            quoted_qty=qty,
            quoted_uom=uom,
            unit_price=price,
            discount_pct=disc,
            tax_rate_pct=tax,
            price_tiers=tiers,
            provenance=prov
        )
        quote_items.append(q_item)

        m_candidate = MatchCandidate(
            candidate_item_id=rfq_line_id or f"ITEM-{idx+1}",
            candidate_sku=spec.get("sku", f"SKU-{idx+1}"),
            candidate_description=desc,
            match_method=MatchMethod.SUPPLIER_SKU_EXACT if status == MatchStatus.EXACT_MATCH else (
                MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL if status in (MatchStatus.HIGH_CONFIDENCE_MATCH, MatchStatus.REVIEW_REQUIRED) else MatchMethod.NO_MATCH
            ),
            match_score=1.0 if status == MatchStatus.EXACT_MATCH else (0.85 if status == MatchStatus.HIGH_CONFIDENCE_MATCH else 0.5),
            match_status=status,
            source_provenance=prov
        )

        matched_items.append(MatchedQuoteItem(
            quote_item=q_item,
            rfq_match=m_candidate,
            item_master_match=m_candidate,
            match_status=status,
            review_reasons=["Requires review"] if status == MatchStatus.REVIEW_REQUIRED else []
        ))


    add_charges = []
    if charges:
        for chg in charges:
            # chg: (type, amount, tax_pct)
            c_type, c_amt, c_tax = chg
            add_charges.append(AdditionalCharge(
                charge_type=c_type,
                amount=Decimal(str(c_amt)),
                tax_rate_pct=Decimal(str(c_tax))
            ))

    p_terms = PaymentTerms(
        raw_text=f"Credit {credit_days} days" if credit_days else (f"Advance {advance_pct}%" if advance_pct else "Net 30"),
        credit_days=credit_days,
        advance_pct=Decimal(str(advance_pct)) if advance_pct else None
    )

    d_terms = DeliveryTerms(
        incoterm=incoterm,
        lead_time_days_default=lead_time_days
    )

    from core.canonical_quote import ExtractionMetadata, ProcessingMode

    meta = ExtractionMetadata(
        source_file_name=f"{supplier_id}.xlsx",
        source_file_hash="dummyhash",
        parser_used="test_parser",
        processing_mode=ProcessingMode.LOCAL
    )

    quote = CanonicalQuote(
        quote_id=f"Q-{supplier_id}",
        supplier_raw_name=supplier_name,
        currency=currency,
        items=quote_items,
        additional_charges=add_charges,
        payment_terms=p_terms,
        delivery_terms=d_terms,
        extraction_metadata=meta
    )


    return SupplierQuoteSubmission(
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        canonical_quote=quote,
        matched_items=matched_items
    )


# =========================================================================
# TESTS
# =========================================================================

def test_three_suppliers_quoting_same_rfq_basic_flow():
    """Scenario 1: 3 suppliers quote same RFQ items with different prices."""
    rfq = make_rfq()
    comparator = QuotationComparator()
    ranking_engine = RankingEngine()

    s1 = make_submission("SUPP-A", "Alpha Bearings", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "120.00", "tax_pct": "18.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "250.00", "tax_pct": "18.0"}
    ])
    s2 = make_submission("SUPP-B", "Beta Industries", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "110.00", "tax_pct": "18.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "260.00", "tax_pct": "18.0"}
    ])
    s3 = make_submission("SUPP-C", "Gamma Supplies", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "130.00", "tax_pct": "18.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "240.00", "tax_pct": "18.0"}
    ])

    comparison = comparator.compare_rfq(rfq, [s1, s2, s3])

    assert len(comparison.item_comparisons) == 2
    assert len(comparison.suppliers) == 3

    # Alpha: (100 * 120 * 1.18) + (500 * 250 * 1.18) = 14,160 + 147,500 = 161,660.00
    # Beta:  (100 * 110 * 1.18) + (500 * 260 * 1.18) = 12,980 + 153,400 = 166,380.00
    # Gamma: (100 * 130 * 1.18) + (500 * 240 * 1.18) = 15,340 + 141,600 = 156,940.00
    assert comparison.suppliers["SUPP-A"].total_quote_landed_base == Decimal("161660.00")
    assert comparison.suppliers["SUPP-B"].total_quote_landed_base == Decimal("166380.00")
    assert comparison.suppliers["SUPP-C"].total_quote_landed_base == Decimal("156940.00")

    report = ranking_engine.generate_ranking_report(comparison)
    assert report.l1_supplier.supplier_id == "SUPP-C"
    assert report.l1_supplier.total_landed_cost_base == Decimal("156940.00")
    assert report.l2_supplier.supplier_id == "SUPP-A"
    assert report.l3_supplier.supplier_id == "SUPP-B"


def test_currency_normalization_with_exchange_rates():
    """Scenario 2 & 3: Multi-currency conversion to base INR."""
    rfq = make_rfq()
    comparator = QuotationComparator()
    ranking_engine = RankingEngine()

    # USD Quote: 100 pcs @ $1.50, 500 mtr @ $3.00
    # Line 1: 100 * 1.50 * 1.0 (tax 0) = $150.00 -> in INR @ 85.0 = 12,750.00
    # Line 2: 500 * 3.00 * 1.0 (tax 0) = $1500.00 -> in INR @ 85.0 = 127,500.00
    # Total = 140,250.00
    s_usd = make_submission("SUPP-USD", "Global US Corp", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "1.50", "tax_pct": "0.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "3.00", "tax_pct": "0.0"}
    ], currency="USD")

    rates = {"USD": Decimal("85.00")}
    comparison = comparator.compare_rfq(rfq, [s_usd], exchange_rates=rates)

    p1 = comparison.item_comparisons["RFQ-ITEM-1"].supplier_prices["SUPP-USD"]
    assert p1.exchange_rate == Decimal("85.00")
    assert p1.line_total_landed_base == Decimal("12750.00")
    assert p1.unit_landed_price_base == Decimal("127.50")

    supp = comparison.suppliers["SUPP-USD"]
    assert supp.total_quote_landed_base == Decimal("140250.00")
    assert supp.is_fully_comparable is True


def test_missing_exchange_rate_blocks_ranking_with_structured_issue():
    """Scenario 12: Missing exchange rate for EUR blocks automatic ranking."""
    rfq = make_rfq()
    comparator = QuotationComparator()
    ranking_engine = RankingEngine()

    s_eur = make_submission("SUPP-EUR", "Euro Tech GmbH", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "1.20", "tax_pct": "0.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "2.80", "tax_pct": "0.0"}
    ], currency="EUR")

    comparison = comparator.compare_rfq(rfq, [s_eur], exchange_rates={})  # Missing EUR rate

    supp = comparison.suppliers["SUPP-EUR"]
    assert supp.is_fully_comparable is False
    assert supp.total_quote_landed_base is None
    assert any(i.code == ComparisonIssueCode.MISSING_EXCHANGE_RATE for i in supp.issues)

    report = ranking_engine.generate_ranking_report(comparison)
    assert report.l1_supplier is None
    assert len(report.ineligible_suppliers) == 1
    assert report.ineligible_suppliers[0].supplier_id == "SUPP-EUR"


def test_different_uom_with_valid_conversion():
    """Scenario 4: Quoted in FT vs requested in MTR (1 MTR = 3.28084 FT)."""
    rfq = make_rfq()  # RFQ Item 2 requests 500 MTR
    comparator = QuotationComparator()

    # Supplier quotes in FT: $1.00 per FT
    # 500 MTR = 1640.42 FT
    # Net taxable = 1640.42 * 1.00 = 1640.42
    # Tax 18% = 295.28
    # Total = 1935.70
    s_uom = make_submission("SUPP-UOM", "Cable Maker Inc", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "100.00", "uom": "PCS", "tax_pct": "0.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "1.00", "uom": "FT", "tax_pct": "18.0"}
    ])

    comparison = comparator.compare_rfq(rfq, [s_uom])
    p2 = comparison.item_comparisons["RFQ-ITEM-2"].supplier_prices["SUPP-UOM"]

    assert p2.is_comparable is True
    assert p2.uom_conversion_factor == Decimal("0.304800")
    # Landed cost per requested MTR in base currency
    assert p2.unit_landed_price_base == Decimal("3.8714")


def test_invalid_uom_conversion_blocked():
    """Scenario 5: Bearing quoted in KG vs requested in PCS is blocked."""
    rfq = make_rfq()
    comparator = QuotationComparator()
    ranking_engine = RankingEngine()

    s_bad_uom = make_submission("SUPP-BAD", "Faulty Trader", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "100.00", "uom": "KG", "match_status": MatchStatus.UOM_INCOMPATIBLE},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "200.00", "uom": "MTR"}
    ])

    comparison = comparator.compare_rfq(rfq, [s_bad_uom])
    p1 = comparison.item_comparisons["RFQ-ITEM-1"].supplier_prices["SUPP-BAD"]

    assert p1.is_comparable is False
    assert any(i.code == ComparisonIssueCode.UOM_INCOMPATIBLE for i in p1.issues)

    report = ranking_engine.generate_ranking_report(comparison)
    assert report.l1_supplier is None
    assert len(report.ineligible_suppliers) == 1


def test_supplier_with_freight_and_packing_charges():
    """Scenario 6 & 7: Landed cost incorporates freight, packing, and charge taxes."""
    rfq = make_rfq()
    comparator = QuotationComparator()

    charges = [
        (ChargeType.FREIGHT, "5000.00", "18.0"),   # 5000 + 18% = 5900
        (ChargeType.PACKING, "1000.00", "18.0"),   # 1000 + 18% = 1180
    ]
    s_chg = make_submission("SUPP-CHG", "Logistics Pros", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "100.00", "tax_pct": "18.0"},  # 100 * 100 * 1.18 = 11,800
        {"rfq_line_id": "RFQ-ITEM-2", "price": "200.00", "tax_pct": "18.0"}   # 500 * 200 * 1.18 = 118,000
    ], charges=charges)

    comparison = comparator.compare_rfq(
        rfq, [s_chg], charge_allocation_method=ChargeAllocationMethod.PROPORTIONAL_LINE_VALUE
    )

    supp = comparison.suppliers["SUPP-CHG"]
    # Total quote landed = 11,800 + 118,000 + 5,900 + 1,180 = 136,880.00
    assert supp.total_quote_landed_base == Decimal("136880.00")
    assert supp.commercial_terms.total_charges_quoted == Decimal("7080.00")


def test_supplier_with_discount_and_compound_tax():
    """Scenario 8 & 9: Discount percentage applied before tax calculation."""
    rfq = make_rfq()
    comparator = QuotationComparator()

    # Price = 200, Disc = 10% -> Net unit = 180. Qty = 100 -> Taxable = 18,000. Tax 18% = 3,240. Landed = 21,240.
    s_disc = make_submission("SUPP-DISC", "Discounted Hardware", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "200.00", "discount_pct": "10.0", "tax_pct": "18.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "300.00", "discount_pct": "5.0", "tax_pct": "18.0"}
    ])

    comparison = comparator.compare_rfq(rfq, [s_disc])
    p1 = comparison.item_comparisons["RFQ-ITEM-1"].supplier_prices["SUPP-DISC"]

    assert p1.net_unit_price_quoted == Decimal("180.00")
    assert p1.line_landed_cost_quoted == Decimal("21240.00")


def test_volume_tier_pricing_applicable_tier_selection():
    """Scenario 10: Selected applicable tier for RFQ requested quantity (100 pcs)."""
    rfq = make_rfq()
    comparator = QuotationComparator()

    tiers = [
        PriceTier(min_qty=Decimal("1.0"), max_qty=Decimal("49.0"), unit_price=Decimal("150.00")),
        PriceTier(min_qty=Decimal("50.0"), max_qty=Decimal("199.0"), unit_price=Decimal("120.00")),
        PriceTier(min_qty=Decimal("200.0"), max_qty=None, unit_price=Decimal("95.00")),
    ]

    s_tier = make_submission("SUPP-TIER", "Tier Supplier Ltd", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "150.00", "tiers": tiers, "tax_pct": "0.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "200.00", "tax_pct": "0.0"}
    ])

    comparison = comparator.compare_rfq(rfq, [s_tier])
    p1 = comparison.item_comparisons["RFQ-ITEM-1"].supplier_prices["SUPP-TIER"]

    assert p1.unit_price_quoted == Decimal("120.00")  # Tier 2 (50-199) chosen for 100 pcs
    assert p1.selected_tier is not None
    assert p1.selected_tier.min_qty == Decimal("50.0")
    assert p1.selected_tier.max_qty == Decimal("199.0")
    assert "Selected tier for quantity" in p1.tier_selection_reason


def test_review_required_item_marked_provisional_and_excluded_from_automatic_l1():
    """Scenario 13: REVIEW_REQUIRED items are provisional and excluded from automatic L1."""
    rfq = make_rfq()
    comparator = QuotationComparator()
    ranking_engine = RankingEngine()

    s_rev = make_submission("SUPP-REV", "Review Needed Co", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "50.00", "match_status": MatchStatus.REVIEW_REQUIRED},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "50.00", "match_status": MatchStatus.EXACT_MATCH}
    ])
    s_valid = make_submission("SUPP-VAL", "Valid Supplier", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "100.00", "match_status": MatchStatus.EXACT_MATCH},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "100.00", "match_status": MatchStatus.EXACT_MATCH}
    ])

    comparison = comparator.compare_rfq(rfq, [s_rev, s_valid])
    p_rev = comparison.item_comparisons["RFQ-ITEM-1"].supplier_prices["SUPP-REV"]

    assert p_rev.is_provisional is True
    assert p_rev.is_comparable is False

    report = ranking_engine.generate_ranking_report(comparison)
    # Valid supplier is L1 even though Review Needed Co has lower quoted unit price
    assert report.l1_supplier.supplier_id == "SUPP-VAL"
    assert report.l2_supplier is None
    assert len(report.ineligible_suppliers) == 1
    assert report.ineligible_suppliers[0].supplier_id == "SUPP-REV"


def test_unmatched_item_excluded_from_comparable_calculations():
    """Scenario 14: UNMATCHED item is flagged and excluded."""
    rfq = make_rfq()
    comparator = QuotationComparator()

    s_unm = make_submission("SUPP-UNM", "Unmatched Trader", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "80.00", "match_status": MatchStatus.UNMATCHED},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "200.00", "match_status": MatchStatus.EXACT_MATCH}
    ])

    comparison = comparator.compare_rfq(rfq, [s_unm])
    p1 = comparison.item_comparisons["RFQ-ITEM-1"].supplier_prices["SUPP-UNM"]

    assert p1.is_comparable is False
    assert any(i.code == ComparisonIssueCode.UNMATCHED_ITEM for i in p1.issues)


def test_multiple_suppliers_identical_prices_deterministic_tie_breaking():
    """Scenario 15: Identical prices break tie deterministically via supplier_id."""
    rfq = make_rfq()
    comparator = QuotationComparator()
    ranking_engine = RankingEngine()

    s_b = make_submission("SUPP-B", "Beta Corp", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "100.00", "tax_pct": "0.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "200.00", "tax_pct": "0.0"}
    ])
    s_a = make_submission("SUPP-A", "Alpha Corp", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "100.00", "tax_pct": "0.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "200.00", "tax_pct": "0.0"}
    ])

    comparison = comparator.compare_rfq(rfq, [s_b, s_a])
    report1 = ranking_engine.generate_ranking_report(comparison)
    report2 = ranking_engine.generate_ranking_report(comparison)

    assert report1.l1_supplier.supplier_id == "SUPP-A"
    assert report1.l2_supplier.supplier_id == "SUPP-B"
    assert report1.l1_supplier.supplier_id == report2.l1_supplier.supplier_id


def test_item_level_split_sourcing_recommendations():
    """Scenario 16: Split sourcing where Supplier A is cheapest for Item 1 and Supplier B is cheapest for Item 2."""
    rfq = make_rfq()
    comparator = QuotationComparator()
    ranking_engine = RankingEngine()

    # Supplier A: Item 1 = 80, Item 2 = 300 -> Total landed = (100*80) + (500*300) = 8,000 + 150,000 = 158,000
    s_a = make_submission("SUPP-A", "Alpha Mechanics", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "80.00", "tax_pct": "0.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "300.00", "tax_pct": "0.0"}
    ])
    # Supplier B: Item 1 = 150, Item 2 = 180 -> Total landed = (100*150) + (500*180) = 15,000 + 90,000 = 105,000
    s_b = make_submission("SUPP-B", "Beta Electrics", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "150.00", "tax_pct": "0.0"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "180.00", "tax_pct": "0.0"}
    ])

    comparison = comparator.compare_rfq(rfq, [s_a, s_b])
    report = ranking_engine.generate_ranking_report(comparison)

    # Single vendor L1 is Beta (105,000 vs 158,000)
    assert report.l1_supplier.supplier_id == "SUPP-B"
    assert report.l1_supplier.total_landed_cost_base == Decimal("105000.00")

    # Item Split Recommendations:
    # Item 1 L1 -> SUPP-A (8,000)
    # Item 2 L1 -> SUPP-B (90,000)
    # Split sourcing total = 8,000 + 90,000 = 98,000
    # Savings vs Single-Vendor L1 = 105,000 - 98,000 = 7,000.00
    rec1 = report.item_split_recommendations["RFQ-ITEM-1"]
    assert rec1.l1_supplier_id == "SUPP-A"
    assert rec1.l1_unit_landed_price_base == Decimal("80.00")
    assert rec1.l2_supplier_id == "SUPP-B"

    rec2 = report.item_split_recommendations["RFQ-ITEM-2"]
    assert rec2.l1_supplier_id == "SUPP-B"
    assert rec2.l1_unit_landed_price_base == Decimal("180.00")
    assert rec2.l2_supplier_id == "SUPP-A"

    assert report.total_split_sourcing_cost_base == Decimal("98000.00")
    assert report.total_split_savings_vs_l1 == Decimal("7000.00")


def test_quote_and_item_invariants_preserved():
    """Invariants: Original CanonicalQuote and QuoteItem are NEVER mutated."""
    rfq = make_rfq()
    comparator = QuotationComparator()

    s = make_submission("SUPP-INV", "Invariant Guard", [
        {"rfq_line_id": "RFQ-ITEM-1", "price": "123.45", "qty": "50.0", "uom": "PCS"},
        {"rfq_line_id": "RFQ-ITEM-2", "price": "678.90", "qty": "200.0", "uom": "MTR"}
    ])

    orig_q_item0_price = s.canonical_quote.items[0].unit_price
    orig_q_item0_qty = s.canonical_quote.items[0].quoted_qty
    orig_cell_ref = s.matched_items[0].quote_item.provenance.cell_ref

    comparison = comparator.compare_rfq(rfq, [s])

    # Assert source objects remain strictly untouched
    assert s.canonical_quote.items[0].unit_price == orig_q_item0_price == Decimal("123.45")
    assert s.canonical_quote.items[0].quoted_qty == orig_q_item0_qty == Decimal("50.0")
    assert s.matched_items[0].quote_item.provenance.cell_ref == orig_cell_ref == "A3"
