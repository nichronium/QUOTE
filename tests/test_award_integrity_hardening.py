import json
import pytest
from decimal import Decimal
from fastapi.testclient import TestClient
from app.main import app
from app import services
from matching.models import MatchStatus


@pytest.fixture
def client():
    return TestClient(app)


def _setup_test_rfq_with_quotes(rfq_id: str):
    """Helper to seed an RFQ with 2 valid quotes and run comparison."""
    # Ensure fresh test isolation
    award_file = services.get_award_file_path(rfq_id)
    if award_file.exists():
        try:
            award_file.unlink()
        except Exception:
            pass

    services.create_rfq(
        rfq_id=rfq_id,
        title=f"Hardened Award RFQ {rfq_id}",
        base_currency="INR",
        items=[
            {
                "sku": "BEAR-6205-2RS",
                "description": "Deep Groove Ball Bearing 6205-2RS",
                "requested_quantity": "100",
                "requested_uom": "PCS",
                "target_price": "150.00"
            },
            {
                "sku": "VALV-BALL-3IN",
                "description": "Ball Valve 3in SS316",
                "requested_quantity": "10",
                "requested_uom": "PCS",
                "target_price": "2500.00"
            }
        ]
    )

    # Quote 1: Supplier Alpha
    q1 = services.create_quote("Alpha Corp", rfq_id=rfq_id)
    csv1 = b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,BEAR-6205-2RS,100,PCS,120.00\n2,VALV-BALL-3IN,10,PCS,2300.00\n"
    services.upload_source_file(q1, "q1.csv", csv1)
    services.run_extraction(q1)
    services.run_matching_for_quote(q1, rfq_id)
    services.resolve_match_candidate(q1, 0, "BEAR-6205-2RS", action="ACCEPT")
    services.resolve_match_candidate(q1, 1, "VALV-BALL-3IN", action="ACCEPT")

    # Quote 2: Supplier Beta
    q2 = services.create_quote("Beta Ind", rfq_id=rfq_id)
    csv2 = b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,BEAR-6205-2RS,100,PCS,110.00\n2,VALV-BALL-3IN,10,PCS,2400.00\n"
    services.upload_source_file(q2, "q2.csv", csv2)
    services.run_extraction(q2)
    services.run_matching_for_quote(q2, rfq_id)
    services.resolve_match_candidate(q2, 0, "BEAR-6205-2RS", action="ACCEPT")
    services.resolve_match_candidate(q2, 1, "VALV-BALL-3IN", action="ACCEPT")

    comp = services.run_rfq_comparison(rfq_id, [q1, q2], base_currency="INR")
    return rfq_id, q1, q2, comp


def test_award_01_full_single_supplier_finalization():
    """TEST 1: Full single-supplier award finalization."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-01")
    proposal = services.build_proposed_award_allocation(rfq_id, scenario="SINGLE_SUPPLIER_L1")
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "FINALIZED"
    assert res["validation_passed"] is True
    assert res["fully_allocated_items_count"] == 2
    assert res["unallocated_items_count"] == 0
    assert Decimal(res["total_awarded_value"]) > 0


def test_award_02_full_split_supplier_finalization():
    """TEST 2: Full split-supplier award finalization across items."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-02")
    proposal = services.build_proposed_award_allocation(rfq_id, scenario="LINE_ITEM_OPTIMAL")
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "FINALIZED"
    assert res["validation_passed"] is True
    assert res["fully_allocated_items_count"] == 2


def test_award_03_over_allocation_rejected():
    """TEST 3: Over-allocation (allocated > requested) rejected server-side."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-03")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # Intentionally over-allocate line 0 (150 > 100)
    proposal["allocations"][0]["supplier_splits"][0]["allocated_qty"] = "150"
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "DRAFT"
    assert res["validation_passed"] is False
    assert any("exceeds required quantity" in err for err in res["validation_errors"])


def test_award_04_negative_quantity_rejected():
    """TEST 4: Negative quantity rejected server-side."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-04")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # Set negative allocated quantity
    proposal["allocations"][0]["supplier_splits"][0]["allocated_qty"] = "-25"
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "DRAFT"
    assert res["validation_passed"] is False
    assert any("Negative allocated quantity" in err for err in res["validation_errors"])


def test_award_05_malformed_quantity_rejected():
    """TEST 5: Malformed non-numeric quantity rejected."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-05")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    proposal["allocations"][0]["supplier_splits"][0]["allocated_qty"] = "twenty_five_pieces"
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "DRAFT"
    assert res["validation_passed"] is False
    assert any("Malformed allocated quantity" in err for err in res["validation_errors"])


def test_award_06_partial_award_rejected_without_acknowledgement():
    """TEST 6: Partial award rejected without buyer acknowledgement."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-06")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # Partial allocation on line 0 (50 out of 100)
    proposal["allocations"][0]["supplier_splits"][0]["allocated_qty"] = "50"
    proposal["buyer_accepted_unallocated"] = False
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "DRAFT"
    assert res["validation_passed"] is False
    assert any("Unallocated quantities present" in err for err in res["validation_errors"])


def test_award_07_partial_award_accepted_with_acknowledgement():
    """TEST 7: Partial award accepted with explicit buyer acknowledgement."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-07")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # Partial allocation on line 0 (50 out of 100)
    proposal["allocations"][0]["supplier_splits"][0]["allocated_qty"] = "50"
    proposal["buyer_accepted_unallocated"] = True
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "FINALIZED"
    assert res["validation_passed"] is True
    assert res["partially_allocated_items_count"] == 1


def test_award_08_invalid_rfq_line_id_rejected():
    """TEST 8: Invalid / foreign RFQ line ID rejected."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-08")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # Inject foreign RFQ line ID
    proposal["allocations"][0]["rfq_line_id"] = "FOREIGN-LINE-999"
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "DRAFT"
    assert res["validation_passed"] is False
    assert any("Invalid RFQ line ID" in err for err in res["validation_errors"])


def test_award_09_cross_rfq_quote_rejected():
    """TEST 9: Cross-RFQ quote/supplier rejected."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-09")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # Inject quote ID from another RFQ
    proposal["allocations"][0]["supplier_splits"][0]["quote_id"] = "Q-OTHER-RFQ-999"
    proposal["allocations"][0]["supplier_splits"][0]["supplier_id"] = "Q-OTHER-RFQ-999"
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "DRAFT"
    assert res["validation_passed"] is False
    assert any("does not have an eligible" in err for err in res["validation_errors"])


def test_award_10_non_comparable_supplier_bid_rejected():
    """TEST 10: Non-comparable / unquoted supplier bid rejected."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-10")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    proposal["allocations"][0]["supplier_splits"][0]["supplier_id"] = "UNKNOWN_SUPPLIER_XYZ"
    proposal["allocations"][0]["supplier_splits"][0]["quote_id"] = "Q-UNKNOWN-XYZ"
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "DRAFT"
    assert res["validation_passed"] is False
    assert any("does not have an eligible" in err for err in res["validation_errors"])


def test_award_11_client_manipulated_unit_price_rejected():
    """TEST 11: Client-manipulated unit price rejected by authoritative comparator check."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-11")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # Tamper with unit price in submission ($1.00 instead of authoritative ~$110.00)
    proposal["allocations"][0]["supplier_splits"][0]["unit_landed_cost"] = "1.00"
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "DRAFT"
    assert res["validation_passed"] is False
    assert any("differs from authoritative comparison price" in err for err in res["validation_errors"])


def test_award_12_client_manipulated_total_ignored_and_recalculated():
    """TEST 12: Client-manipulated split and grand totals are ignored and recalculated deterministically."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-12")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # Tamper with total awarded value and split value
    proposal["total_awarded_value"] = "999999.00"
    proposal["allocations"][0]["supplier_splits"][0]["split_value"] = "10.00"
    proposal["allocations"][0]["total_line_value"] = "10.00"
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res["status"] == "FINALIZED"
    assert res["validation_passed"] is True
    # Server-calculated total should be authoritative (~34,000 INR or ~35,000 INR depending on L1)
    assert res["total_awarded_value"] != "999999.00"
    assert Decimal(res["total_awarded_value"]) == sum(Decimal(a["total_line_value"]) for a in res["allocations"])


def test_award_13_direct_finalize_of_already_finalized_rejected():
    """TEST 13: Direct finalization of an already FINALIZED award rejected without reopening."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-13")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # 1. First finalize
    res1 = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res1["status"] == "FINALIZED"
    
    # 2. Attempt direct re-finalize
    res2 = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res2["validation_passed"] is False
    assert any("already FINALIZED and locked" in err for err in res2["validation_errors"])


def test_award_14_explicit_reopen_modify_finalize_with_audit_trail():
    """TEST 14: Explicit reopen -> modify -> finalize works and preserves audit trail."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-14")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    # 1. Finalize
    res1 = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert res1["status"] == "FINALIZED"
    
    # 2. Explicit Reopen with reason
    reopened = services.reopen_award_decision(rfq_id, reason="Buyer requested split allocation revision", reopened_by="Procurement Specialist")
    assert reopened["status"] == "REOPENED"
    assert len(reopened["reopen_history"]) >= 1
    assert reopened["reopen_history"][0]["previous_status"] == "FINALIZED"
    
    # 3. Modify allocations & Finalize again
    proposal2 = services.build_proposed_award_allocation(rfq_id, scenario="LINE_ITEM_OPTIMAL")
    res2 = services.save_award_decision(rfq_id, proposal2, is_finalized=True)
    assert res2["status"] == "FINALIZED"
    assert res2["validation_passed"] is True


def test_award_15_finalized_award_remains_unchanged_on_refresh(client: TestClient):
    """TEST 15: Finalized award remains unchanged after page refresh / revisit."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-15")
    proposal = services.build_proposed_award_allocation(rfq_id)
    services.save_award_decision(rfq_id, proposal, is_finalized=True)
    
    # GET /rfqs/{rfq_id}/award
    res_page = client.get(f"/rfqs/{rfq_id}/award")
    assert res_page.status_code == 200
    assert "AWARD FINALIZED" in res_page.text
    
    # Verify persisted record unchanged
    persisted = services.get_award_decision(rfq_id)
    assert persisted["status"] == "FINALIZED"


def test_award_16_finalized_award_remains_unchanged_when_newer_comparison_exists():
    """TEST 16: Finalized award remains locked even when a newer comparison exists."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-16")
    proposal = services.build_proposed_award_allocation(rfq_id)
    services.save_award_decision(rfq_id, proposal, is_finalized=True)
    
    old_award = services.get_award_decision(rfq_id)
    old_comp_id = old_award["comparison_id"]
    
    # Re-run comparison (generates newer comparison snapshot)
    comp2 = services.run_rfq_comparison(rfq_id, [q1, q2], base_currency="INR")
    
    # Award retrieval should still return the locked finalized record
    award_check = services.build_proposed_award_allocation(rfq_id)
    assert award_check["status"] == "FINALIZED"
    assert award_check["comparison_id"] == old_comp_id


def test_award_17_newer_comparison_warning_surfaced(client: TestClient):
    """TEST 17: Newer comparison warning is surfaced when newer comparison snapshot exists."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-17")
    proposal = services.build_proposed_award_allocation(rfq_id)
    services.save_award_decision(rfq_id, proposal, is_finalized=True)
    
    # Generate newer comparison
    import time
    time.sleep(0.05)
    comp2 = services.run_rfq_comparison(rfq_id, [q1, q2], base_currency="INR")
    
    res_page = client.get(f"/rfqs/{rfq_id}/award")
    assert res_page.status_code == 200
    assert "Newer Commercial Evaluation Available" in res_page.text


def test_award_18_deterministic_decimal_split_and_grand_total():
    """TEST 18: Deterministic Decimal calculation for split values and grand totals."""
    rfq_id, q1, q2, comp = _setup_test_rfq_with_quotes("RFQ-AWD-18")
    proposal = services.build_proposed_award_allocation(rfq_id)
    
    res = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    
    for a in res["allocations"]:
        expected_line_val = Decimal("0")
        for sp in a["supplier_splits"]:
            expected_sp_val = Decimal(str(sp["allocated_qty"])) * Decimal(str(sp["unit_landed_cost"]))
            assert Decimal(str(sp["split_value"])) == expected_sp_val
            expected_line_val += expected_sp_val
        assert Decimal(str(a["total_line_value"])) == expected_line_val


def test_award_19_stale_comparison_refreshed_before_award():
    """TEST 19: Stale comparison is refreshed before Award uses it."""
    rfq_id = "RFQ-AWD-19"
    services.create_rfq(rfq_id, "Stale Comp Test", "INR", [{"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "10", "requested_uom": "PCS"}])
    
    qid = services.create_quote("Supp 19", rfq_id=rfq_id)
    services.upload_source_file(qid, "19.csv", b"Line Number,Description,Quoted Qty,UOM,Unit Price\n1,Generic Bearing,10,PCS,100.00\n")
    services.run_extraction(qid)
    services.run_matching_for_quote(qid, rfq_id)
    
    # Run comparison while unresolved
    comp = services.run_rfq_comparison(rfq_id, [qid], base_currency="INR")
    
    # Now resolve match
    services.resolve_match_candidate(qid, 0, "BEAR-6205-2RS", action="ACCEPT")
    
    # Award proposal must load refreshed comparison with comparable bid
    award_prop = services.build_proposed_award_allocation(rfq_id)
    assert len(award_prop["allocations"][0]["available_bids"]) > 0
    assert award_prop["allocations"][0]["available_bids"][0]["is_comparable"] is True


def test_award_20_multi_rfq_isolation():
    """TEST 20: Award cannot mix RFQ lines or quotes from different RFQs."""
    rfq_a, q_a1, q_a2, comp_a = _setup_test_rfq_with_quotes("RFQ-AWD-20A")
    rfq_b, q_b1, q_b2, comp_b = _setup_test_rfq_with_quotes("RFQ-AWD-20B")
    
    proposal_a = services.build_proposed_award_allocation(rfq_a)
    
    # Try to insert a quote from RFQ B into RFQ A's award submission
    proposal_a["allocations"][0]["supplier_splits"][0]["quote_id"] = q_b1
    proposal_a["allocations"][0]["supplier_splits"][0]["supplier_id"] = q_b1
    
    res = services.save_award_decision(rfq_a, proposal_a, is_finalized=True)
    assert res["status"] == "DRAFT"
    assert res["validation_passed"] is False
    assert any("does not have an eligible" in err for err in res["validation_errors"])
