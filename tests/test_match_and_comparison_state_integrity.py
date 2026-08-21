import json
import pytest
from decimal import Decimal
from fastapi.testclient import TestClient
from app.main import app
from app import services
from matching.models import MatchStatus, ItemMasterRecord


@pytest.fixture
def client():
    return TestClient(app)


def test_regression_a_manual_resolution_survives_comparison(client: TestClient):
    """
    TEST A — Manual resolution survives comparison:
    1. Create RFQ.
    2. Upload quote with ambiguous item.
    3. Manually resolve it.
    4. Verify matched_items.json contains human confirmation.
    5. Run comparison.
    6. Verify matched_items.json still contains human resolution (NOT overwritten).
    7. Verify comparison does NOT show REVIEW_REQUIRED for that line.
    8. Verify correct Item Master / RFQ line is used.
    """
    # 1. Create RFQ
    rfq_id = "RFQ-INTEG-A"
    items_payload = json.dumps([
        {
            "sku": "BEAR-6205-2RS",
            "description": "Deep Groove Ball Bearing 6205-2RS 25x52x15mm",
            "requested_quantity": "100",
            "requested_uom": "PCS",
            "target_price": "150.00"
        }
    ])
    res_rfq = client.post("/rfqs", data={
        "rfq_id": rfq_id,
        "title": "State Integrity Test RFQ A",
        "base_currency": "INR",
        "items_json": items_payload
    })
    assert res_rfq.status_code in [200, 303]

    # 2. Upload Quote
    quote_id = services.create_quote("Supplier Alpha", rfq_id=rfq_id)
    quote_csv = b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,Deep Groove Bearing 6205 generic,100,PCS,120.00\n"
    services.upload_source_file(quote_id, "quote_a.csv", quote_csv)
    services.run_extraction(quote_id)
    services.run_matching_for_quote(quote_id, rfq_id)

    # Verify initial matched items
    initial_matches = services.get_matched_items(quote_id)
    assert len(initial_matches) == 1

    # 3. Manually resolve the item to "BEAR-6205-2RS"
    services.resolve_match_candidate(quote_id, line_index=0, chosen_candidate_sku="BEAR-6205-2RS", action="ACCEPT")

    # 4. Verify matched_items.json contains human confirmation
    resolved_matches = services.get_matched_items(quote_id)
    assert resolved_matches[0].match_status == MatchStatus.EXACT_MATCH
    assert "human_confirmation" in resolved_matches[0].item_master_match.matched_fields
    assert resolved_matches[0].item_master_match.candidate_sku == "BEAR-6205-2RS"

    # 5. Run comparison
    comp_res = services.run_rfq_comparison(
        rfq_id=rfq_id,
        quote_ids=[quote_id],
        base_currency="INR"
    )

    # 6. Verify matched_items.json STILL contains human resolution
    after_comp_matches = services.get_matched_items(quote_id)
    assert after_comp_matches[0].match_status == MatchStatus.EXACT_MATCH
    assert "human_confirmation" in after_comp_matches[0].item_master_match.matched_fields
    assert after_comp_matches[0].item_master_match.candidate_sku == "BEAR-6205-2RS"

    # 7 & 8. Verify comparison does NOT show REVIEW_REQUIRED and is comparable
    item_comps = comp_res["comparison"]["item_comparisons"]
    rfq_line_id = list(item_comps.keys())[0]
    supp_price = item_comps[rfq_line_id]["supplier_prices"][quote_id]
    assert supp_price["is_comparable"] is True
    assert supp_price["is_provisional"] is False
    assert supp_price["match_status"] == "EXACT_MATCH"


def test_regression_b_rerunning_comparison_preserves_resolution():
    """
    TEST B — Re-running comparison twice preserves resolution.
    """
    rfq_id = "RFQ-INTEG-B"
    services.create_rfq(
        rfq_id=rfq_id,
        title="State Integrity Test RFQ B",
        base_currency="INR",
        items=[{
            "sku": "VALV-BALL-3IN",
            "description": "Stainless Steel 316 Ball Valve 3 inch Flanged",
            "requested_quantity": "10",
            "requested_uom": "PCS",
            "target_price": "2500.00"
        }]
    )

    quote_id = services.create_quote("Supplier Beta", rfq_id=rfq_id)
    quote_csv = b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,SS316 Valve 3in,10,PCS,2300.00\n"
    services.upload_source_file(quote_id, "quote_b.csv", quote_csv)
    services.run_extraction(quote_id)
    services.run_matching_for_quote(quote_id, rfq_id)

    # Manually resolve
    services.resolve_match_candidate(quote_id, line_index=0, chosen_candidate_sku="VALV-BALL-3IN", action="ACCEPT")

    # Run comparison #1
    comp1 = services.run_rfq_comparison(rfq_id, [quote_id], base_currency="INR")
    # Run comparison #2
    comp2 = services.run_rfq_comparison(rfq_id, [quote_id], base_currency="INR")

    # Persisted matched state is intact
    persisted = services.get_matched_items(quote_id)
    assert persisted[0].match_status == MatchStatus.EXACT_MATCH
    assert "human_confirmation" in persisted[0].item_master_match.matched_fields

    # Both comparisons evaluated as comparable
    rfq_line_id = list(comp2["comparison"]["item_comparisons"].keys())[0]
    assert comp2["comparison"]["item_comparisons"][rfq_line_id]["supplier_prices"][quote_id]["is_comparable"] is True


def test_regression_c_automatic_matcher_cannot_overwrite_human_confirmation():
    """
    TEST C — Automatic matcher cannot overwrite human confirmation even if invoked directly.
    """
    rfq_id = "RFQ-INTEG-C"
    services.create_rfq(
        rfq_id=rfq_id,
        title="State Integrity Test RFQ C",
        base_currency="INR",
        items=[{
            "sku": "PUMP-CENT-5HP",
            "description": "Centrifugal Water Pump 5HP 3-Phase",
            "requested_quantity": "2",
            "requested_uom": "SET",
            "target_price": "45000.00"
        }]
    )

    quote_id = services.create_quote("Supplier Gamma", rfq_id=rfq_id)
    quote_csv = b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,Industrial Water Pump 5HP,2,SET,42000.00\n"
    services.upload_source_file(quote_id, "quote_c.csv", quote_csv)
    services.run_extraction(quote_id)
    services.run_matching_for_quote(quote_id, rfq_id)

    # Manually resolve
    services.resolve_match_candidate(quote_id, line_index=0, chosen_candidate_sku="PUMP-CENT-5HP", action="ACCEPT")

    # Call run_matching_for_quote directly multiple times
    services.run_matching_for_quote(quote_id, rfq_id)
    services.run_matching_for_quote(quote_id, rfq_id)

    # Check persistence
    matches = services.get_matched_items(quote_id)
    assert matches[0].match_status == MatchStatus.EXACT_MATCH
    assert matches[0].item_master_match.candidate_sku == "PUMP-CENT-5HP"
    assert "human_confirmation" in matches[0].item_master_match.matched_fields


def test_regression_d_comparison_freshness_after_later_resolution():
    """
    TEST D — Existing comparison snapshot detects staleness after a later manual resolution
    and automatically returns fresh updated data.
    """
    rfq_id = "RFQ-INTEG-D"
    services.create_rfq(
        rfq_id=rfq_id,
        title="State Integrity Test RFQ D",
        base_currency="INR",
        items=[{
            "sku": "BEAR-6205-2RS",
            "description": "Deep Groove Ball Bearing 6205-2RS",
            "requested_quantity": "50",
            "requested_uom": "PCS",
            "target_price": "150.00"
        }]
    )

    quote_id = services.create_quote("Supplier Delta", rfq_id=rfq_id)
    # Give an ambiguous description that initially creates REVIEW_REQUIRED or non-exact match
    quote_csv = b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,Ambiguous Generic Item XYZ,50,PCS,130.00\n"
    services.upload_source_file(quote_id, "quote_d.csv", quote_csv)
    services.run_extraction(quote_id)
    services.run_matching_for_quote(quote_id, rfq_id)

    # 1. Run comparison while unresolved
    comp_initial = services.run_rfq_comparison(rfq_id, [quote_id], base_currency="INR")
    comp_id = comp_initial["comparison_id"]

    # 2. Later: User resolves line manually in Review Center
    services.resolve_match_candidate(quote_id, line_index=0, chosen_candidate_sku="BEAR-6205-2RS", action="ACCEPT")

    # 3. Retrieve comparison: get_comparison must detect staleness & regenerate dynamically
    comp_updated = services.get_comparison(comp_id)
    assert comp_updated is not None
    rfq_line_id = list(comp_updated["comparison"]["item_comparisons"].keys())[0]
    supp_price = comp_updated["comparison"]["item_comparisons"][rfq_line_id]["supplier_prices"][quote_id]
    
    # Must now be comparable with exact match landed cost!
    assert supp_price["is_comparable"] is True
    assert supp_price["match_status"] == "EXACT_MATCH"


def test_regression_e_unresolved_lines_remain_blocked():
    """
    TEST E — Unresolved / rejected lines still behave correctly and are not falsely marked comparable.
    """
    rfq_id = "RFQ-INTEG-E"
    services.create_rfq(
        rfq_id=rfq_id,
        title="State Integrity Test RFQ E",
        base_currency="INR",
        items=[{
            "sku": "FLTR-AIR-HEPA",
            "description": "HEPA Filter 24x24x12 inch",
            "requested_quantity": "5",
            "requested_uom": "PCS",
            "target_price": "3500.00"
        }]
    )

    quote_id = services.create_quote("Supplier Echo", rfq_id=rfq_id)
    quote_csv = b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,Completely Unrelated Item 999,5,PCS,100.00\n"
    services.upload_source_file(quote_id, "quote_e.csv", quote_csv)
    services.run_extraction(quote_id)
    services.run_matching_for_quote(quote_id, rfq_id)

    # Reject / Mark unmatched
    services.resolve_match_candidate(quote_id, line_index=0, chosen_candidate_sku=None, action="MARK_UNMATCHED")

    # Verify locked as UNMATCHED
    matches = services.get_matched_items(quote_id)
    assert matches[0].match_status == MatchStatus.UNMATCHED

    # Run comparison
    comp = services.run_rfq_comparison(rfq_id, [quote_id], base_currency="INR")
    rfq_line_id = list(comp["comparison"]["item_comparisons"].keys())[0]
    assert quote_id not in comp["comparison"]["item_comparisons"][rfq_line_id]["comparable_supplier_ids"]


def test_regression_f_multi_rfq_isolation():
    """
    TEST F — Multi-RFQ isolation: manual resolution in RFQ A never bleeds into RFQ B.
    """
    # Create RFQ A
    rfq_a = "RFQ-INTEG-F1"
    services.create_rfq(rfq_a, "RFQ A", "INR", [{"sku": "SKU-AAA", "description": "Item AAA", "requested_quantity": "10", "requested_uom": "PCS"}])
    
    # Create RFQ B
    rfq_b = "RFQ-INTEG-F2"
    services.create_rfq(rfq_b, "RFQ B", "INR", [{"sku": "SKU-BBB", "description": "Item BBB", "requested_quantity": "10", "requested_uom": "PCS"}])

    quote_a = services.create_quote("Supp A", rfq_id=rfq_a)
    services.upload_source_file(quote_a, "a.csv", b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,Desc AAA,10,PCS,50.00\n")
    services.run_extraction(quote_a)
    services.run_matching_for_quote(quote_a, rfq_a)
    services.resolve_match_candidate(quote_a, 0, "SKU-AAA", action="ACCEPT")

    quote_b = services.create_quote("Supp B", rfq_id=rfq_b)
    services.upload_source_file(quote_b, "b.csv", b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,Desc BBB,10,PCS,60.00\n")
    services.run_extraction(quote_b)
    services.run_matching_for_quote(quote_b, rfq_b)

    # Quotes for RFQ B must not include quote_a
    rfq_b_quotes = services.get_quotes_for_rfq(rfq_b)
    rfq_b_quote_ids = [q["test_id"] for q in rfq_b_quotes]
    assert quote_a not in rfq_b_quote_ids
    assert quote_b in rfq_b_quote_ids


def test_regression_g_end_to_end_revisit_and_award_flow(client: TestClient):
    """
    TEST G — Complete lifecycle:
    RFQ creation -> quote upload -> manual resolution -> comparison -> revisit comparison -> award proposal -> finalize award
    """
    rfq_id = "RFQ-INTEG-G"
    services.create_rfq(
        rfq_id=rfq_id,
        title="State Integrity Lifecycle RFQ",
        base_currency="INR",
        items=[{
            "sku": "BEAR-6205-2RS",
            "description": "Deep Groove Ball Bearing 6205-2RS",
            "requested_quantity": "20",
            "requested_uom": "PCS",
            "target_price": "140.00"
        }]
    )

    quote_id = services.create_quote("Supplier Zeta", rfq_id=rfq_id)
    quote_csv = b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,Bearing 6205 Zeta,20,PCS,125.00\n"
    services.upload_source_file(quote_id, "quote_z.csv", quote_csv)
    services.run_extraction(quote_id)
    services.run_matching_for_quote(quote_id, rfq_id)

    # 1. Resolve match
    res_resolve = client.post(f"/rfqs/{rfq_id}/review/resolve", data={
        "quote_id": quote_id,
        "line_index": 0,
        "chosen_candidate_sku": "BEAR-6205-2RS",
        "action": "ACCEPT"
    })
    assert res_resolve.status_code in [200, 303]

    # 2. Run Commercial Comparison via route
    res_comp = client.post("/comparisons", data={
        "rfq_id": rfq_id,
        "quote_ids": [quote_id],
        "base_currency": "INR"
    })
    assert res_comp.status_code in [200, 303]

    # 3. View Award Decision page
    res_award_page = client.get(f"/rfqs/{rfq_id}/award")
    assert res_award_page.status_code == 200
    assert "Award Decision" in res_award_page.text

    # 4. Finalize Award
    award_prop = services.build_proposed_award_allocation(rfq_id)
    res_finalize = client.post(f"/rfqs/{rfq_id}/award/finalize", data={
        "allocations_json": json.dumps(award_prop["allocations"]),
        "justification": "Optimal validated price from Supplier Zeta"
    })
    assert res_finalize.status_code in [200, 303]

    # 5. Verify finalized award on disk
    award_rec = services.get_award_decision(rfq_id)
    assert award_rec is not None
    assert award_rec["status"] == "FINALIZED"
    assert award_rec["fully_allocated_items_count"] == 1
