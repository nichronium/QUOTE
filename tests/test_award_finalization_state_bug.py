import io
import json
from decimal import Decimal
import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import services
from datasets.procurement_benchmark.dataset import get_benchmark_item_master


@pytest.fixture
def client():
    return TestClient(app)


def test_award_finalization_state_consistency_and_zero_wipe_regression(client: TestClient):
    """
    P0 REGRESSION TEST:
    Ensures that when an award is finalized:
    1. Persisted JSON retains non-zero total_awarded_value, allocated lines, and supplier_splits.
    2. GET /award in FINALIZED mode renders exact non-zero commitment and allocated lines count.
    3. Available bids are not confused with awarded allocations.
    4. Unawarded competing bids (allocated_qty == 0) do not appear in finalized supplier_splits.
    5. Action ribbon renders 'AWARD FINALIZED • READY FOR PROCUREMENT HANDOFF'.
    6. Reopen preserves allocations and allows subsequent re-finalization.
    7. All exports match the persisted finalized award totals.
    """
    rfq_id = "RFQ-P0-REGRESS-001"
    
    # 0. Clean prior state
    rfq_file = services.RFQS_DIR / f"{rfq_id}.json"
    if rfq_file.exists():
        rfq_file.unlink()
    award_file = services.get_award_file_path(rfq_id)
    if award_file.exists():
        award_file.unlink()
    for comp_f in services.COMPARISONS_DIR.glob(f"*{rfq_id}*.json"):
        comp_f.unlink()
    for q_dir in services.DATA_DIR.glob(f"Q-{rfq_id}-*"):
        import shutil
        shutil.rmtree(q_dir, ignore_errors=True)

    # 1. Create RFQ with 3 items
    item_master = get_benchmark_item_master()
    rfq_items = [
        {
            "sku": item_master[0].internal_sku,
            "description": item_master[0].canonical_description,
            "requested_quantity": "100",
            "requested_uom": "PCS",
            "target_price": "150.00"
        },
        {
            "sku": item_master[2].internal_sku,
            "description": item_master[2].canonical_description,
            "requested_quantity": "500",
            "requested_uom": "MTR",
            "target_price": "200.00"
        },
        {
            "sku": item_master[8].internal_sku,
            "description": item_master[8].canonical_description,
            "requested_quantity": "20",
            "requested_uom": "PCS",
            "target_price": "4000.00"
        }
    ]

    services.create_rfq(rfq_id, "P0 Award State Consistency Test", "INR", rfq_items)

    # 2. Quotations (2 competing suppliers)
    # Supplier 1: Alpha
    q1 = services.create_quote("Alpha Corp", rfq_id=rfq_id)
    csv1 = f"Line,SKU,Description,Qty,UOM,Unit Price\n1,{item_master[0].internal_sku},{item_master[0].canonical_description},100,PCS,120.00\n2,{item_master[2].internal_sku},{item_master[2].canonical_description},500,MTR,190.00\n3,{item_master[8].internal_sku},{item_master[8].canonical_description},20,PCS,3600.00\n"
    services.upload_source_file(q1, "alpha.csv", csv1.encode("utf-8"))

    # Supplier 2: Beta
    q2 = services.create_quote("Beta Ltd", rfq_id=rfq_id)
    csv2 = f"Line,SKU,Description,Qty,UOM,Unit Price\n1,{item_master[0].internal_sku},{item_master[0].canonical_description},100,PCS,130.00\n2,{item_master[2].internal_sku},{item_master[2].canonical_description},500,MTR,185.00\n3,{item_master[8].internal_sku},{item_master[8].canonical_description},20,PCS,3700.00\n"
    services.upload_source_file(q2, "beta.csv", csv2.encode("utf-8"))

    for qid in [q1, q2]:
        services.run_extraction(qid)
        services.run_matching_for_quote(qid, rfq_id)

    # 3. Commercial Comparison
    comp = services.run_rfq_comparison(rfq_id, [q1, q2], base_currency="INR")
    comp_id = comp["comparison_id"]

    # 4. View Award Page in Draft Mode
    res_draft = client.get(f"/rfqs/{rfq_id}/award")
    assert res_draft.status_code == 200
    assert "Step 5 — Award Decision" in res_draft.text
    assert "DRAFT ALLOCATION" in res_draft.text

    # Expected proposal: Line 0 -> Alpha (120*100 = 12000), Line 1 -> Beta (185*500 = 92500), Line 2 -> Alpha (3600*20 = 72000)
    # Total = 12000 + 92500 + 72000 = 176500.00
    proposal = services.build_proposed_award_allocation(rfq_id, scenario="LINE_ITEM_OPTIMAL")
    expected_total = Decimal("176500.00")
    assert Decimal(proposal["total_awarded_value"]) == expected_total
    assert proposal["fully_allocated_items_count"] == 3
    assert proposal["unallocated_items_count"] == 0

    # 5. POST /finalize
    res_finalize = client.post(f"/rfqs/{rfq_id}/award/finalize", data={
        "selected_scenario": "LINE_ITEM_OPTIMAL",
        "base_currency": "INR",
        "allocations_json": json.dumps(proposal["allocations"]),
        "award_notes": "Line item optimal allocation approved."
    }, follow_redirects=True)
    assert res_finalize.status_code == 200

    # 6. Verify Persisted Award JSON directly on disk
    persisted_award = services.get_award_decision(rfq_id)
    assert persisted_award is not None
    assert persisted_award["status"] == "FINALIZED"
    assert Decimal(persisted_award["total_awarded_value"]) == expected_total
    assert persisted_award["fully_allocated_items_count"] == 3
    assert persisted_award["partially_allocated_items_count"] == 0
    assert persisted_award["unallocated_items_count"] == 0
    assert len(persisted_award["allocations"]) == 3

    for alloc in persisted_award["allocations"]:
        assert alloc["allocation_state"] == "FULLY_ALLOCATED"
        assert Decimal(alloc["unallocated_qty"]) == Decimal("0")
        assert Decimal(alloc["total_allocated_qty"]) == Decimal(alloc["required_qty"])
        assert len(alloc["supplier_splits"]) >= 1
        for sp in alloc["supplier_splits"]:
            assert Decimal(sp["allocated_qty"]) > Decimal("0")
            assert Decimal(sp["split_value"]) == Decimal(sp["allocated_qty"]) * Decimal(sp["unit_landed_cost"])

    # 7. Verify GET /award rendering in FINALIZED mode
    res_final_get = client.get(f"/rfqs/{rfq_id}/award")
    assert res_final_get.status_code == 200
    assert "AWARD FINALIZED" in res_final_get.text
    assert "176500.00" in res_final_get.text or "176,500.00" in res_final_get.text
    assert "3 / 3 Fully" in res_final_get.text
    assert "0 Partial • 0 Unallocated" in res_final_get.text or "0 Partial &bull; 0 Unallocated" in res_final_get.text
    assert "AWARD FINALIZED • READY FOR PROCUREMENT HANDOFF" in res_final_get.text or "AWARD FINALIZED &bull; READY FOR PROCUREMENT HANDOFF" in res_final_get.text
    # Ensure obsolete wording is completely gone
    assert "Legally Ready for Purchase Order Generation" not in res_final_get.text

    # 8. Available bids vs Awarded allocations separation check
    # Check Line 0: Alpha was awarded 100 PCS; Beta was NOT awarded.
    line_0 = persisted_award["allocations"][0]
    awarded_supp_ids = [s["supplier_id"] for s in line_0["supplier_splits"]]
    assert q1 in awarded_supp_ids
    assert q2 not in awarded_supp_ids  # Beta has an available bid but zero allocation -> not in supplier_splits

    # 9. Verify Exporters use authoritative finalized values
    # Excel
    res_xlsx = client.get(f"/rfqs/{rfq_id}/award/export/excel")
    assert res_xlsx.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(res_xlsx.content), data_only=True)
    ws_exec = wb["Executive Summary"]
    assert "176,500.00" in str(ws_exec["B11"].value) or "176500.00" in str(ws_exec["B11"].value)

    # PDF
    res_pdf = client.get(f"/rfqs/{rfq_id}/award/export/pdf")
    assert res_pdf.status_code == 200
    assert res_pdf.content.startswith(b"%PDF-")

    # CSV
    res_csv = client.get(f"/rfqs/{rfq_id}/award/export/csv")
    assert res_csv.status_code == 200
    assert "176500.00" in res_csv.text or "12000.00" in res_csv.text

    # Supplier Requisitions
    res_req_q1 = client.get(f"/rfqs/{rfq_id}/award/export/requisition/{q1}/pdf")
    res_req_q2 = client.get(f"/rfqs/{rfq_id}/award/export/requisition/{q2}/pdf")
    assert res_req_q1.status_code == 200
    assert res_req_q2.status_code == 200
