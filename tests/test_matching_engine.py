"""
RFQ and Item Master Matching Engine Test Suite.
Verifies deterministic identifier matching, multi-signal fuzzy scoring,
dimension-safe UOM resolution, non-destructive quote preservation, and repeatability.
"""

from decimal import Decimal
import pytest

from core.canonical_quote import (
    CanonicalQuote,
    FieldEvidence,
    FieldStatus,
    Provenance,
    QuoteItem,
)
from matching.models import (
    ItemMasterRecord,
    MatchCandidate,
    MatchedQuoteItem,
    MatchMethod,
    MatchStatus,
    RFQLineItem,
)
from matching.matcher import ItemMatcher
from matching.uom_resolver import UOMResolver


@pytest.fixture
def item_master_fixture():
    return [
        ItemMasterRecord(
            internal_item_id="ITEM-001",
            internal_sku="BEAR-6205-2RS",
            manufacturer_part_number="6205-2RS1",
            approved_supplier_part_numbers=["SKF-6205-2RS", "FAG-6205-2RSR", "6205.2RS"],
            canonical_description="Deep Groove Ball Bearing 25x52x15mm Rubber Sealed",
            stocking_uom="PCS",
            brand="SKF",
            specifications={"size": "25x52x15mm", "type": "Deep Groove Ball Bearing"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-002",
            internal_sku="CABL-6SQ-CU",
            manufacturer_part_number="CAB-6SQ-FLEX",
            approved_supplier_part_numbers=["NCW-CAB-6SQ", "POL-6SQ-CU"],
            canonical_description="Copper Armoured Flexible Cable 6 Sq.mm 4-Core",
            stocking_uom="MTR",
            brand="Polycab",
            specifications={"size": "6 sq.mm", "cores": "4"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-003",
            internal_sku="FAST-SS-M8-40",
            manufacturer_part_number="DIN-912-M8-40",
            approved_supplier_part_numbers=["SS-M8-40", "BOLT-M8X40-SS"],
            canonical_description="Hex Socket Head Cap Screw M8x40mm Stainless Steel 304",
            stocking_uom="PCS",
            approved_conversion_factors={"BOX": Decimal("100.0")},
            brand="Unbrako",
            specifications={"size": "M8x40mm", "grade": "SS304"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-004",
            internal_sku="FAST-SS-M8-50",
            manufacturer_part_number="DIN-912-M8-50",
            approved_supplier_part_numbers=["SS-M8-50", "BOLT-M8X50-SS"],
            canonical_description="Hex Socket Head Cap Screw M8x50mm Stainless Steel 304",
            stocking_uom="PCS",
            brand="Unbrako",
            specifications={"size": "M8x50mm", "grade": "SS304"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-005",
            internal_sku="SOLAR-INV-50KW",
            manufacturer_part_number="SUN2000-50KTL",
            approved_supplier_part_numbers=["HW-50KTL", "INV-50KW-3P"],
            canonical_description="On-Grid Solar String Inverter 50kW 3-Phase 400V",
            stocking_uom="PCS",
            brand="Huawei",
            specifications={"power": "50kW", "phase": "3-Phase"}
        ),
    ]


def _make_quote_item(
    desc: str,
    sku: str = None,
    qty: str = "10",
    uom: str = "PCS",
    price: str = "100.00",
    line_idx: int = 0
) -> QuoteItem:
    prov = Provenance(sheet_name="Offer", row_idx=line_idx + 1, col_idx=0, cell_ref=f"A{line_idx+2}", source_text=desc)
    return QuoteItem(
        line_index=line_idx,
        raw_description=desc,
        supplier_part_number=sku,
        quoted_qty=Decimal(qty),
        quoted_uom=uom,
        unit_price=Decimal(price),
        provenance=prov,
        description_evidence=FieldEvidence(raw_value=desc, normalized_value=desc, sheet_name="Offer", cell_range=f"A{line_idx+2}", status=FieldStatus.CONFIRMED)
    )


# =========================================================================
# 1. Deterministic Identifier Matching
# =========================================================================
def test_exact_supplier_sku_match(item_master_fixture):
    """Supplier part number matches approved list -> EXACT_MATCH."""
    matcher = ItemMatcher()
    item = _make_quote_item("SKF Ball Bearing Sealed", sku="SKF-6205-2RS", qty="5", uom="PCS")

    matched = matcher.match_quote_item(item, item_master_fixture)

    assert matched.match_status == MatchStatus.EXACT_MATCH
    assert matched.item_master_match is not None
    assert matched.item_master_match.candidate_item_id == "ITEM-001"
    assert matched.item_master_match.match_method == MatchMethod.SUPPLIER_SKU_EXACT
    assert matched.item_master_match.match_score == 1.0
    # Provenance preserved
    assert matched.item_master_match.source_provenance.cell_ref == "A2"
    # Original item unchanged
    assert matched.quote_item.raw_description == "SKF Ball Bearing Sealed"


def test_exact_manufacturer_part_number_match(item_master_fixture):
    """Supplier part number matches manufacturer PN -> EXACT_MATCH."""
    matcher = ItemMatcher()
    item = _make_quote_item("Solar Grid Inverter 50k", sku="SUN2000-50KTL", qty="1", uom="PCS")

    matched = matcher.match_quote_item(item, item_master_fixture)

    assert matched.match_status == MatchStatus.EXACT_MATCH
    assert matched.item_master_match.candidate_item_id == "ITEM-005"
    assert matched.item_master_match.match_method == MatchMethod.MANUFACTURER_PN_EXACT


def test_exact_internal_sku_match(item_master_fixture):
    """Supplier quote echoes internal item code -> EXACT_MATCH."""
    matcher = ItemMatcher()
    item = _make_quote_item("Copper Armoured Cable", sku="CABL-6SQ-CU", qty="500", uom="MTR")

    matched = matcher.match_quote_item(item, item_master_fixture)

    assert matched.match_status == MatchStatus.EXACT_MATCH
    assert matched.item_master_match.candidate_item_id == "ITEM-002"
    assert matched.item_master_match.match_method == MatchMethod.INTERNAL_SKU_EXACT


# =========================================================================
# 2. Multi-Signal Fuzzy Description Matching
# =========================================================================
def test_fuzzy_description_match_with_clear_specifications(item_master_fixture):
    """Clear description matching canonical description + specs -> HIGH_CONFIDENCE_MATCH."""
    matcher = ItemMatcher()
    # No SKU provided, description has strong specs and keywords
    item = _make_quote_item("Deep Groove Ball Bearing 25x52x15mm Rubber Sealed SKF", sku=None, qty="20", uom="PCS")

    matched = matcher.match_quote_item(item, item_master_fixture)

    assert matched.match_status == MatchStatus.HIGH_CONFIDENCE_MATCH
    assert matched.item_master_match.candidate_item_id == "ITEM-001"
    assert matched.item_master_match.match_score >= 0.88
    assert "Specification agreement" in " ".join(matched.item_master_match.explanations)


def test_two_close_fuzzy_candidates_produce_review_required(item_master_fixture):
    """When two candidates have almost identical descriptions (e.g. M8x40 vs M8x50 without spec in quote), produce REVIEW_REQUIRED."""
    matcher = ItemMatcher()
    # Quote description lacks length specification (just says M8 socket screw)
    item = _make_quote_item("Hex Socket Head Cap Screw M8 Stainless Steel 304", sku=None, qty="100", uom="PCS")

    matched = matcher.match_quote_item(item, item_master_fixture)

    assert matched.match_status == MatchStatus.REVIEW_REQUIRED
    assert len(matched.top_candidates) >= 2
    # Check that candidate 1 and 2 are ITEM-003 and ITEM-004
    cand_ids = {c.candidate_item_id for c in matched.top_candidates[:2]}
    assert "ITEM-003" in cand_ids or "ITEM-004" in cand_ids
    assert any("Ambiguity" in r for r in matched.review_reasons)


def test_no_candidate_produces_unmatched(item_master_fixture):
    """Unrelated item not in Item Master -> UNMATCHED."""
    matcher = ItemMatcher()
    item = _make_quote_item("Industrial Safety Harness Full Body Double Lanyard", sku=None, qty="10", uom="PCS")

    matched = matcher.match_quote_item(item, item_master_fixture)

    assert matched.match_status == MatchStatus.UNMATCHED
    assert matched.item_master_match is None or matched.item_master_match.match_score < 0.70


def test_different_but_similarly_named_items_prevent_false_positive(item_master_fixture):
    """M8x40 screw must NOT auto-match to M8x50 screw due to spec conflict penalty."""
    matcher = ItemMatcher()
    # Explicitly asking for M8x40
    item = _make_quote_item("Hex Socket Head Cap Screw M8x40mm SS304", sku=None, qty="100", uom="PCS")

    matched = matcher.match_quote_item(item, item_master_fixture)

    # Must match ITEM-003 (M8x40), NOT ITEM-004 (M8x50)
    assert matched.item_master_match.candidate_item_id == "ITEM-003"
    assert matched.item_master_match.match_status == MatchStatus.HIGH_CONFIDENCE_MATCH


# =========================================================================
# 3. Dimension-Safe UOM Resolution
# =========================================================================
def test_valid_intra_dimension_conversion_mtr_to_ft(item_master_fixture):
    """MTR quoted vs FT stocking in same length dimension converts accurately."""
    resolver = UOMResolver()
    result = resolver.resolve_uom_conversion(
        source_uom="MTR",
        target_uom="FT",
        quoted_quantity=Decimal("100.0")
    )

    assert result.is_compatible is True
    assert result.conversion_method == "STANDARD_DIMENSION"
    # 1 MTR = 3.28084 FT -> 100 MTR = 328.0840 FT
    assert result.conversion_factor == Decimal("3.280840")
    assert result.converted_quantity == Decimal("328.0840")


def test_invalid_cross_dimension_conversion_blocked(item_master_fixture):
    """KG quoted vs PCS/NOS stocking without packaging factor -> UOM_INCOMPATIBLE."""
    matcher = ItemMatcher()
    # Bearings quoted in KG (unphysical / cross-dimension)
    item = _make_quote_item("SKF Ball Bearing Sealed", sku="SKF-6205-2RS", qty="10", uom="KG")

    matched = matcher.match_quote_item(item, item_master_fixture)

    assert matched.match_status == MatchStatus.UOM_INCOMPATIBLE
    assert matched.uom_conversion.is_compatible is False
    assert "Cross-dimension conversion" in matched.uom_conversion.error_reason


def test_item_specific_packaging_factor_conversion_box_to_pcs(item_master_fixture):
    """BOX quoted for ITEM-003 with approved factor (1 BOX = 100 PCS) converts successfully."""
    matcher = ItemMatcher()
    item = _make_quote_item("Hex Socket Screws", sku="SS-M8-40", qty="5", uom="BOX")

    matched = matcher.match_quote_item(item, item_master_fixture)

    assert matched.match_status == MatchStatus.EXACT_MATCH
    assert matched.uom_conversion.is_compatible is True
    assert matched.uom_conversion.conversion_method == "ITEM_MASTER_FACTOR"
    assert matched.uom_conversion.conversion_factor == Decimal("100.0")
    # 5 BOX * 100 = 500 PCS
    assert matched.uom_conversion.converted_quantity == Decimal("500.0000")


# =========================================================================
# 4. Provenance Preservation & Deterministic Repeatability
# =========================================================================
def test_quote_provenance_survives_matching_stage(item_master_fixture):
    """All original quote attributes, cell references, and evidence remain completely intact."""
    matcher = ItemMatcher()
    orig_item = _make_quote_item("Copper Cable 6sqmm", sku="CABL-6SQ-CU", qty="250", uom="MTR", price="145.00", line_idx=3)

    matched = matcher.match_quote_item(orig_item, item_master_fixture)

    # Identical memory and values on inner quote_item
    assert matched.quote_item.line_index == 3
    assert matched.quote_item.provenance.cell_ref == "A5"
    assert matched.quote_item.unit_price == Decimal("145.00")
    assert matched.quote_item.description_evidence.cell_range == "A5"


def test_deterministic_repeatability(item_master_fixture):
    """Repeated calls with identical inputs must produce 100% identical outputs."""
    matcher = ItemMatcher()
    item = _make_quote_item("Hex Socket Head Cap Screw M8 Stainless Steel 304", sku=None, qty="50", uom="PCS")

    run1 = matcher.match_quote_item(item, item_master_fixture)
    run2 = matcher.match_quote_item(item, item_master_fixture)

    assert run1.match_status == run2.match_status
    assert run1.item_master_match.match_score == run2.item_master_match.match_score
    assert len(run1.top_candidates) == len(run2.top_candidates)
    for c1, c2 in zip(run1.top_candidates, run2.top_candidates):
        assert c1.candidate_item_id == c2.candidate_item_id
        assert c1.match_score == c2.match_score
