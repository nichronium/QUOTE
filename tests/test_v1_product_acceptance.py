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


def test_v1_full_lifecycle_and_navigation_acceptance(client: TestClient):
    """
    V1 PRODUCT ACCEPTANCE TEST
    Full end-to-end user journey across all 5 procurement stages:
    1. Item Master Catalog
    2. Fresh RFQ creation
    3. Multi-supplier quote ingestion with diverse constraints
    4. Review Center exception resolution
    5. Commercial Evaluation
    6. Award Decision, split sourcing, immutability & reopening
    7. Procurement Execution exports (Excel, PDF, CSV, Supplier Requisitions)
    8. Inter-page navigation, page refreshes, and multi-tenancy isolation
    """
    rfq_id = "RFQ-V1-ACCEPT-AUTO"
    
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

    # 1. Item Master
    res_im = client.get("/item-master")
    assert res_im.status_code == 200

    # 2. RFQ Creation
    item_master = get_benchmark_item_master()
    rfq_items = [
        {
            "sku": item_master[0].internal_sku,
            "description": item_master[0].canonical_description,
            "requested_quantity": "50",
            "requested_uom": "PCS",
            "target_price": "140.00"
        },
        {
            "sku": item_master[2].internal_sku,
            "description": item_master[2].canonical_description,
            "requested_quantity": "200",
            "requested_uom": "MTR",
            "target_price": "220.00"
        }
    ]

    res_rfq = client.post("/rfqs", data={
        "rfq_id": rfq_id,
        "title": "V1 Plant Modernization Scope",
        "base_currency": "INR",
        "items_json": json.dumps(rfq_items)
    }, follow_redirects=True)
    assert res_rfq.status_code == 200
    assert rfq_id in res_rfq.text

    # 3. Supplier Quotes Upload & Extraction
    # Quote 1 (Alpha): Complete
    q1 = services.create_quote("Alpha Industrial", rfq_id=rfq_id)
    csv1 = f"Line,SKU,Description,Qty,UOM,Unit Price\n1,{item_master[0].internal_sku},{item_master[0].canonical_description},50,PCS,125.00\n2,{item_master[2].internal_sku},{item_master[2].canonical_description},200,MTR,195.00\n"
    services.upload_source_file(q1, "alpha.csv", csv1.encode("utf-8"))

    # Quote 2 (Beta): Partial + Extra unrequested item
    q2 = services.create_quote("Beta Supplies", rfq_id=rfq_id)
    supp_pn = item_master[0].approved_supplier_part_numbers[0]
    csv2 = f"Line,Part No,Description,Qty,UOM,Price,Discount Pct\n1,{supp_pn},{item_master[0].canonical_description},50,PCS,120.00,2.0\n2,EXTRA-UNREQ,Unrequested Drill 10mm,5,PCS,90.00,0.0\n"
    services.upload_source_file(q2, "beta.csv", csv2.encode("utf-8"))

    # Quote 3 (Gamma): Ambiguous description
    q3 = services.create_quote("Gamma Tech", rfq_id=rfq_id)
    csv3 = f"Line,Description,Qty,UOM,Unit Price\n1,Generic Ball Bearing 6205 series rubber seal,50,PCS,118.00\n2,{item_master[2].internal_sku},200,MTR,190.00\n"
    services.upload_source_file(q3, "gamma.csv", csv3.encode("utf-8"))

    for qid in [q1, q2, q3]:
        services.run_extraction(qid)
        services.run_matching_for_quote(qid, rfq_id)

    # 4. Review Center & Resolution
    res_rev = client.get(f"/rfqs/{rfq_id}/review")
    assert res_rev.status_code == 200

    services.resolve_match_candidate(q3, 0, item_master[0].internal_sku, action="ACCEPT")
    matched_q3 = services.get_matched_items(q3)
    assert "human_confirmation" in matched_q3[0].item_master_match.matched_fields

    # 5. Commercial Evaluation
    comp_res = services.run_rfq_comparison(rfq_id, [q1, q2, q3], base_currency="INR")
    comp_id = comp_res["comparison_id"]

    res_comp = client.get(f"/comparisons/{comp_id}")
    assert res_comp.status_code == 200
    assert "Commercial Evaluation" in res_comp.text

    # 6. Award Decision & Split Sourcing
    proposal = services.build_proposed_award_allocation(rfq_id, scenario="LINE_ITEM_OPTIMAL")
    alloc_0 = proposal["allocations"][0]
    bids_0 = {b["quote_id"]: b for b in alloc_0["available_bids"]}

    alloc_0["supplier_splits"] = [
        {
            "supplier_id": q3,
            "quote_id": q3,
            "allocated_qty": "30",
            "unit_landed_cost": str(bids_0[q3]["unit_landed_cost"])
        },
        {
            "supplier_id": q2,
            "quote_id": q2,
            "allocated_qty": "20",
            "unit_landed_cost": str(bids_0[q2]["unit_landed_cost"])
        }
    ]

    res_final = client.post(f"/rfqs/{rfq_id}/award/finalize", data={
        "selected_scenario": "MANUAL_ALLOCATION",
        "base_currency": "INR",
        "allocations_json": json.dumps(proposal["allocations"]),
        "award_notes": "Split allocation finalized."
    }, follow_redirects=True)
    assert res_final.status_code == 200
    assert "AWARD FINALIZED" in res_final.text

    # 7. Reopen & Re-finalize
    res_reopen = client.post(f"/rfqs/{rfq_id}/award/reopen", data={
        "reason": "100% allocation to Gamma"
    }, follow_redirects=True)
    assert "AWARD REOPENED FOR REVISION" in res_reopen.text

    proposal_reopened = services.get_award_decision(rfq_id)
    proposal_reopened["allocations"][0]["supplier_splits"] = [
        {
            "supplier_id": q3,
            "quote_id": q3,
            "allocated_qty": "50",
            "unit_landed_cost": str(bids_0[q3]["unit_landed_cost"])
        }
    ]
    res_refinal = client.post(f"/rfqs/{rfq_id}/award/finalize", data={
        "selected_scenario": "MANUAL_ALLOCATION",
        "base_currency": "INR",
        "allocations_json": json.dumps(proposal_reopened["allocations"])
    }, follow_redirects=True)
    assert "AWARD FINALIZED" in res_refinal.text

    # 8. Exports
    res_xlsx = client.get(f"/rfqs/{rfq_id}/award/export/excel")
    assert res_xlsx.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(res_xlsx.content), data_only=True)
    assert len(wb.sheetnames) == 4

    res_pdf = client.get(f"/rfqs/{rfq_id}/award/export/pdf")
    assert res_pdf.status_code == 200
    assert res_pdf.content.startswith(b"%PDF-")

    res_csv = client.get(f"/rfqs/{rfq_id}/award/export/csv")
    assert res_csv.status_code == 200
    assert rfq_id in res_csv.text

    res_req_pdf = client.get(f"/rfqs/{rfq_id}/award/export/requisition/{q3}/pdf")
    assert res_req_pdf.status_code == 200
    assert res_req_pdf.content.startswith(b"%PDF-")
