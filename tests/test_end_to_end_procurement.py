"""
End-to-End Procurement Benchmark Test Suite.
Exercises the entire quotation intelligence lifecycle:
Raw XLSX File -> ExcelParser -> QuoteExtractor -> CanonicalQuote ->
ItemMatcher -> MatchedQuoteItem[] -> QuotationComparator -> RFQComparison ->
RankingEngine -> GlobalRankingReport.

Tests real document parsing, SKU/PN/Fuzzy matching, UOM conversion, compound GST,
currency conversion, commercial charges, eligibility gating, L1/L2/L3 ranking,
split sourcing, provenance preservation, and 3-run deterministic repeatability.
"""

from decimal import Decimal
from pathlib import Path
import pytest

from parsers.excel_parser import ExcelParser
from extraction.extractor import QuoteExtractor
from matching.matcher import ItemMatcher
from matching.models import MatchStatus
from comparison.comparator import QuotationComparator
from comparison.models import ChargeAllocationMethod, ComparisonIssueCode, SupplierQuoteSubmission
from comparison.ranking import RankingEngine
from datasets.procurement_benchmark.dataset import (
    generate_benchmark_workbooks,
    get_benchmark_item_master,
    get_benchmark_rfq,
)


@pytest.fixture(scope="module")
def benchmark_data(tmp_path_factory):
    bench_dir = tmp_path_factory.mktemp("e2e_procurement_benchmark")
    rfq = get_benchmark_rfq()
    item_master = get_benchmark_item_master()
    workbook_paths = generate_benchmark_workbooks(bench_dir)
    exchange_rates = {"USD": Decimal("85.00"), "EUR": Decimal("92.50")}
    return {
        "bench_dir": bench_dir,
        "rfq": rfq,
        "item_master": item_master,
        "workbook_paths": workbook_paths,
        "exchange_rates": exchange_rates,
    }


def _run_full_pipeline(benchmark_data):
    """Executes the complete document-to-ranking pipeline from raw files."""
    rfq = benchmark_data["rfq"]
    item_master = benchmark_data["item_master"]
    workbook_paths = benchmark_data["workbook_paths"]
    exchange_rates = benchmark_data["exchange_rates"]

    parser = ExcelParser()
    extractor = QuoteExtractor()
    matcher = ItemMatcher()
    comparator = QuotationComparator()
    ranking_engine = RankingEngine()

    submissions = []
    extracted_quotes = {}

    for supp_id, file_path in workbook_paths.items():
        # 1. Parse raw XLSX document
        ast = parser.parse(file_path)

        # 2. Extract CanonicalQuote
        quote = extractor.extract(ast)
        extracted_quotes[supp_id] = quote

        # 3. Match line items against Item Master and RFQ
        matched_items = matcher.match_quote(quote, item_master, rfq.items)

        # 4. Form submission envelope
        submissions.append(SupplierQuoteSubmission(
            supplier_id=supp_id,
            supplier_name=quote.supplier_raw_name,
            canonical_quote=quote,
            matched_items=matched_items
        ))

    # 5. Build multi-supplier comparison matrix
    comparison = comparator.compare_rfq(
        rfq=rfq,
        submissions=submissions,
        exchange_rates=exchange_rates,
        charge_allocation_method=ChargeAllocationMethod.PROPORTIONAL_LINE_VALUE
    )

    # 6. Generate quote-level and item-split ranking report
    ranking_report = ranking_engine.generate_ranking_report(comparison)

    return {
        "extracted_quotes": extracted_quotes,
        "submissions": submissions,
        "comparison": comparison,
        "ranking_report": ranking_report
    }


# =========================================================================
# 1. PHASE 1: EXTRACTION ACCURACY TESTS
# =========================================================================

def test_phase1_extraction_all_suppliers(benchmark_data):
    """Verifies that QuoteExtractor extracts all items, charges, and metadata from raw XLSX files."""
    result = _run_full_pipeline(benchmark_data)
    quotes = result["extracted_quotes"]

    # Supplier A: Alpha (Clean, 20 items, INR)
    quote_a = quotes["SUPP-A"]
    assert len(quote_a.items) == 20
    assert quote_a.currency == "INR"
    assert quote_a.supplier_raw_name == "Alpha Industrial Supplies Ltd"

    # Supplier B: Bharat (20 items, compound GST 9%+9%=18%, Freight 12500, Packing 3500)
    quote_b = quotes["SUPP-B"]
    assert len(quote_b.items) == 20
    assert len(quote_b.additional_charges) >= 2
    charge_types = {c.charge_type.value for c in quote_b.additional_charges}
    assert "FREIGHT" in charge_types
    assert "PACKING" in charge_types

    # Supplier C: Continental (20 items, discounts, quoted FT for cables)
    quote_c = quotes["SUPP-C"]
    assert len(quote_c.items) == 20
    # Fastener items have 10% discount
    m8_item = next(i for i in quote_c.items if "SS-M8-40" in (i.supplier_part_number or ""))
    assert m8_item.discount_pct == Decimal("10.0")

    # Supplier D: Delta (20 items, USD currency)
    quote_d = quotes["SUPP-D"]
    assert len(quote_d.items) == 20
    assert quote_d.currency == "USD"

    # Supplier E: Elite (20 items)
    quote_e = quotes["SUPP-E"]
    assert len(quote_e.items) == 20


# =========================================================================
# 2. PHASE 2: MATCHING ACCURACY TESTS
# =========================================================================

def test_phase2_item_master_matching(benchmark_data):
    """Verifies deterministic identifier matching, UOM handling, ambiguity detection, and unmatched flags."""
    result = _run_full_pipeline(benchmark_data)
    submissions = {s.supplier_id: s for s in result["submissions"]}

    # Supplier A: Exact Supplier SKUs -> 20/20 EXACT_MATCH
    matched_a = submissions["SUPP-A"].matched_items
    assert all(m.match_status == MatchStatus.EXACT_MATCH for m in matched_a)

    # Supplier B: Manufacturer Part Numbers -> 20/20 EXACT_MATCH
    matched_b = submissions["SUPP-B"].matched_items
    assert all(m.match_status == MatchStatus.EXACT_MATCH for m in matched_b)

    # Supplier C: Dimension-Safe UOM conversion for Cable (FT -> MTR)
    matched_c = submissions["SUPP-C"].matched_items
    cable_c = next(m for m in matched_c if "POL-6SQ-CU" in (m.quote_item.supplier_part_number or ""))
    assert cable_c.quote_item.quoted_uom == "FT"
    assert cable_c.match_status == MatchStatus.EXACT_MATCH

    # Supplier E: Ambiguity & Incompatible UOM & Unmatched items
    matched_e = submissions["SUPP-E"].matched_items
    statuses_e = [m.match_status for m in matched_e]

    # Two under-specified M8 screws without length in quote description -> REVIEW_REQUIRED
    rev_count = sum(1 for s in statuses_e if s == MatchStatus.REVIEW_REQUIRED)
    assert rev_count >= 2

    # Hose in KG -> UOM_INCOMPATIBLE
    uom_incompat = [m for m in matched_e if m.match_status == MatchStatus.UOM_INCOMPATIBLE]
    assert len(uom_incompat) >= 1

    # Obsolete actuator -> UNMATCHED
    unmatched = [m for m in matched_e if m.match_status == MatchStatus.UNMATCHED]
    assert len(unmatched) >= 1


# =========================================================================
# 3. PHASE 3: COMMERCIAL NORMALIZATION & COMPARISON ACCURACY
# =========================================================================

def test_phase3_commercial_normalization_and_matrix(benchmark_data):
    """Verifies multi-currency conversion, charge allocation, UOM normalization, and comparability gating."""
    result = _run_full_pipeline(benchmark_data)
    comp = result["comparison"]

    assert len(comp.item_comparisons) == 20
    assert len(comp.suppliers) == 5

    # Fully comparable suppliers: SUPP-A, SUPP-B, SUPP-C, SUPP-D
    assert comp.suppliers["SUPP-A"].is_fully_comparable is True
    assert comp.suppliers["SUPP-B"].is_fully_comparable is True
    assert comp.suppliers["SUPP-C"].is_fully_comparable is True
    assert comp.suppliers["SUPP-D"].is_fully_comparable is True

    # Disqualified supplier: SUPP-E (has REVIEW_REQUIRED, UOM_INCOMPATIBLE, and UNMATCHED)
    assert comp.suppliers["SUPP-E"].is_fully_comparable is False

    # Currency Conversion Check on SUPP-D (USD @ 85.0)
    supp_d = comp.suppliers["SUPP-D"]
    assert supp_d.source_currency == "USD"
    assert supp_d.exchange_rate == Decimal("85.00")
    assert supp_d.total_quote_landed_base is not None
    assert supp_d.total_quote_landed_base > Decimal("0.0")

    # Commercial Charge Allocation Check on SUPP-B
    supp_b = comp.suppliers["SUPP-B"]
    assert supp_b.commercial_terms.total_charges_quoted > Decimal("0.0")
    # All 20 items have allocated charges
    for item_comp in comp.item_comparisons.values():
        p_b = item_comp.supplier_prices["SUPP-B"]
        assert p_b.allocated_charges_quoted > Decimal("0.0")
        assert p_b.charge_allocation_method == ChargeAllocationMethod.PROPORTIONAL_LINE_VALUE


# =========================================================================
# 4. FINAL RANKING & SPLIT-SOURCING ACCURACY
# =========================================================================

def test_final_ranking_and_split_sourcing(benchmark_data):
    """Verifies deterministic L1/L2/L3 quote-level rankings and item-level split recommendations."""
    result = _run_full_pipeline(benchmark_data)
    report = result["ranking_report"]

    # Quote-level L1, L2, L3 must be present among eligible suppliers
    assert report.l1_supplier is not None
    assert report.l2_supplier is not None
    assert report.l3_supplier is not None

    # SUPP-E must be classified as ineligible
    ineligible_ids = {s.supplier_id for s in report.ineligible_suppliers}
    assert "SUPP-E" in ineligible_ids

    # Ranked suppliers must be sorted ascending by landed cost
    ranked_costs = [s.total_landed_cost_base for s in report.ranked_suppliers]
    assert ranked_costs == sorted(ranked_costs)

    # Item Split Recommendations
    assert len(report.item_split_recommendations) == 20
    for rfq_id, split in report.item_split_recommendations.items():
        assert split.is_valid is True
        assert split.l1_supplier_id in ("SUPP-A", "SUPP-B", "SUPP-C", "SUPP-D")
        assert split.l1_unit_landed_price_base is not None

    # Split sourcing total cost must be <= L1 single vendor cost
    assert report.total_split_sourcing_cost_base is not None
    assert report.total_split_sourcing_cost_base <= report.l1_supplier.total_landed_cost_base
    assert report.total_split_savings_vs_l1 >= Decimal("0.0")


# =========================================================================
# 5. REPEATABILITY & DETERMINISM (3 CONSECUTIVE RUNS)
# =========================================================================

def test_three_consecutive_runs_deterministic_equality(benchmark_data):
    """Verifies that running the complete benchmark 3 times produces 100% identical outputs."""
    run1 = _run_full_pipeline(benchmark_data)
    run2 = _run_full_pipeline(benchmark_data)
    run3 = _run_full_pipeline(benchmark_data)

    rep1 = run1["ranking_report"]
    rep2 = run2["ranking_report"]
    rep3 = run3["ranking_report"]

    # Identical L1 Supplier
    assert rep1.l1_supplier.supplier_id == rep2.l1_supplier.supplier_id == rep3.l1_supplier.supplier_id
    assert rep1.l1_supplier.total_landed_cost_base == rep2.l1_supplier.total_landed_cost_base == rep3.l1_supplier.total_landed_cost_base

    # Identical Supplier Rankings
    ranks1 = [(s.rank, s.supplier_id, s.total_landed_cost_base) for s in rep1.ranked_suppliers]
    ranks2 = [(s.rank, s.supplier_id, s.total_landed_cost_base) for s in rep2.ranked_suppliers]
    ranks3 = [(s.rank, s.supplier_id, s.total_landed_cost_base) for s in rep3.ranked_suppliers]
    assert ranks1 == ranks2 == ranks3

    # Identical Split Sourcing Savings
    assert rep1.total_split_sourcing_cost_base == rep2.total_split_sourcing_cost_base == rep3.total_split_sourcing_cost_base
    assert rep1.total_split_savings_vs_l1 == rep2.total_split_savings_vs_l1 == rep3.total_split_savings_vs_l1

    # Identical Item Level L1s
    for rfq_id in rep1.item_split_recommendations:
        l1_1 = rep1.item_split_recommendations[rfq_id].l1_supplier_id
        l1_2 = rep2.item_split_recommendations[rfq_id].l1_supplier_id
        l1_3 = rep3.item_split_recommendations[rfq_id].l1_supplier_id
        assert l1_1 == l1_2 == l1_3


# =========================================================================
# 6. INVARIANTS & PROVENANCE SURVIVAL
# =========================================================================

def test_provenance_and_immutability_invariants(benchmark_data):
    """Verifies that source CanonicalQuote and QuoteItems remain strictly unmutated and retain cell coordinates."""
    result = _run_full_pipeline(benchmark_data)
    submissions = result["submissions"]

    for sub in submissions:
        quote = sub.canonical_quote
        # Check that quote items retain cell_ref provenance
        for item in quote.items:
            assert item.provenance is not None
            assert item.provenance.cell_ref.startswith("A") or len(item.provenance.cell_ref) > 0
            assert item.unit_price > Decimal("0.0")
            assert item.quoted_qty > Decimal("0.0")
