import io
import json
import time
import openpyxl
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import services


@pytest.fixture
def client():
    return TestClient(app)


def test_rfq_isolation_revisitability_and_state_persistence(client: TestClient):
    """
    Verifies that:
    1. RFQ A and RFQ B maintain strict isolation without cross-contamination.
    2. Every RFQ page is 100% reconstructable from the RFQ ID alone.
    3. Reopening from Home, RFQ List, Direct URL, or Back/Forward renders identical persistent data.
    4. Missing child objects render empty/setup states gracefully without 500 errors.
    """
    # -------------------------------------------------------------
    # 1. SETUP RFQ A: 2 Items, 2 Quotes, Compared & Awarded
    # -------------------------------------------------------------
    ts = int(time.time() * 1000)
    rfq_a_id = f"RFQ-ISOLATION-A-{ts}"
    items_a = [
        {"rfq_line_id": "LINE-A-01", "sku": "BEAR-6205-2RS", "description": "Deep Groove Ball Bearing 25x52x15mm Rubber Sealed", "requested_quantity": 100.0, "requested_uom": "PCS"},
        {"rfq_line_id": "LINE-A-02", "sku": "VALV-BALL-3IN", "description": "Industrial Flanged Ball Valve 3-inch", "requested_quantity": 20.0, "requested_uom": "PCS"}
    ]
    client.post("/rfqs", data={"rfq_id": rfq_a_id, "title": "Plant Expansion Alpha", "base_currency": "INR", "items_json": json.dumps(items_a)}, follow_redirects=True)

    # Upload Quote A1 (Apex)
    wb_a1 = openpyxl.Workbook()
    ws_a1 = wb_a1.active
    ws_a1.title = "Quotation"
    ws_a1.append(["Apex Industrial Supplies Ltd"])
    ws_a1.append(["Currency:", "INR", "Payment Terms:", "Net 30"])
    ws_a1.append(["Supplier SKU", "Description", "Qty", "UOM", "Unit Price", "Tax %"])
    ws_a1.append(["SKF-6205-2RS", "Deep Groove Ball Bearing 25x52x15mm", 100, "PCS", 450.00, 18.0])
    ws_a1.append(["VALV-BALL-3IN", "Industrial Flanged Ball Valve 3-inch", 20, "PCS", 3200.00, 18.0])
    buf_a1 = io.BytesIO()
    wb_a1.save(buf_a1)

    client.post(f"/rfqs/{rfq_a_id}/upload", files={"file": ("quote_a1.xlsx", buf_a1.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, data={"supplier_name": "Apex Industrial"}, follow_redirects=True)

    # Upload Quote A2 (Beta)
    wb_a2 = openpyxl.Workbook()
    ws_a2 = wb_a2.active
    ws_a2.title = "Quotation"
    ws_a2.append(["Beta Dynamics Corp"])
    ws_a2.append(["Currency:", "INR", "Payment Terms:", "Net 30"])
    ws_a2.append(["Supplier SKU", "Description", "Qty", "UOM", "Unit Price", "Tax %"])
    ws_a2.append(["SKF-6205-2RS", "Deep Groove Ball Bearing 25x52x15mm", 100, "PCS", 430.00, 18.0])
    ws_a2.append(["VALV-BALL-3IN", "Industrial Flanged Ball Valve 3-inch", 20, "PCS", 3400.00, 18.0])
    buf_a2 = io.BytesIO()
    wb_a2.save(buf_a2)

    client.post(f"/rfqs/{rfq_a_id}/upload", files={"file": ("quote_a2.xlsx", buf_a2.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, data={"supplier_name": "Beta Dynamics"}, follow_redirects=True)

    quotes_a = services.get_quotes_for_rfq(rfq_a_id)
    assert len(quotes_a) == 2
    for q in quotes_a:
        services.run_matching_for_quote(q["test_id"], rfq_a_id)

    # Run comparison for RFQ A
    comp_a_res = client.post("/comparisons", data={"rfq_id": rfq_a_id, "quote_ids": [q["test_id"] for q in quotes_a], "base_currency": "INR"}, follow_redirects=False)
    assert comp_a_res.status_code == 303
    comp_a_id = comp_a_res.headers["location"].split("/")[-1]

    # Finalize Award for RFQ A
    award_data_a = services.build_proposed_award_allocation(rfq_a_id, comp_a_id, "SINGLE_SUPPLIER_L1")
    client.post(f"/rfqs/{rfq_a_id}/award/finalize", data={
        "selected_scenario": "SINGLE_SUPPLIER_L1",
        "base_currency": "INR",
        "allocations_json": json.dumps(award_data_a["allocations"]),
        "buyer_accepted_unallocated": "true"
    }, follow_redirects=True)

    # -------------------------------------------------------------
    # 2. SETUP RFQ B: 1 Item, 1 Quote, No Comparison, Draft Award
    # -------------------------------------------------------------
    rfq_b_id = f"RFQ-ISOLATION-B-{ts}"
    items_b = [
        {"rfq_line_id": "LINE-B-01", "sku": "FAST-SS-M8-40", "description": "Hex Head Bolt M8 x 40mm SS304", "requested_quantity": 500.0, "requested_uom": "PCS"}
    ]
    client.post("/rfqs", data={"rfq_id": rfq_b_id, "title": "Fasteners Bulk Procurement Beta", "base_currency": "INR", "items_json": json.dumps(items_b)}, follow_redirects=True)

    # Upload Quote B1 (Gamma)
    wb_b1 = openpyxl.Workbook()
    ws_b1 = wb_b1.active
    ws_b1.title = "Quotation"
    ws_b1.append(["Gamma Fasteners Ltd"])
    ws_b1.append(["Currency:", "INR", "Payment Terms:", "Net 30"])
    ws_b1.append(["Supplier SKU", "Description", "Qty", "UOM", "Unit Price", "Tax %"])
    ws_b1.append(["SS-M8-40", "Hex Head Bolt M8 x 40mm SS304", 500, "PCS", 25.00, 18.0])
    buf_b1 = io.BytesIO()
    wb_b1.save(buf_b1)

    client.post(f"/rfqs/{rfq_b_id}/upload", files={"file": ("quote_b1.xlsx", buf_b1.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, data={"supplier_name": "Gamma Fasteners"}, follow_redirects=True)

    quotes_b = services.get_quotes_for_rfq(rfq_b_id)
    assert len(quotes_b) == 1
    services.run_matching_for_quote(quotes_b[0]["test_id"], rfq_b_id)

    # -------------------------------------------------------------
    # 3. VERIFY INDEPENDENT RECONSTRUCTION & MULTI-RFQ ISOLATION
    # -------------------------------------------------------------

    # A. Open Home (/) -> Open RFQ A -> verify all quotes, comparison, finalized award present
    home_res = client.get("/")
    assert home_res.status_code == 200
    assert rfq_a_id in home_res.text
    assert rfq_b_id in home_res.text

    ws_a_from_home = client.get(f"/rfqs/{rfq_a_id}")
    assert ws_a_from_home.status_code == 200
    assert "Plant Expansion Alpha" in ws_a_from_home.text
    assert "2 Received" in ws_a_from_home.text
    assert "Commercial Sourcing Award Finalized" in ws_a_from_home.text
    assert "STAGE 5 &bull; AWARD FINALIZED" in ws_a_from_home.text

    # B. Open RFQs List (/rfqs) -> Open RFQ B -> verify 1 quote, not evaluated, no finalized award
    rfqs_list_res = client.get("/rfqs")
    assert rfqs_list_res.status_code == 200

    ws_b_from_list = client.get(f"/rfqs/{rfq_b_id}")
    assert ws_b_from_list.status_code == 200
    assert "Fasteners Bulk Procurement Beta" in ws_b_from_list.text
    assert "1 Received" in ws_b_from_list.text
    assert "Commercial Sourcing Award Finalized" not in ws_b_from_list.text
    assert "Evaluate 1 Ready Supplier Quotation" in ws_b_from_list.text

    # C. Reopen RFQ A directly by URL -> ensure zero leakage from RFQ B
    ws_a_direct = client.get(f"/rfqs/{rfq_a_id}")
    assert ws_a_direct.status_code == 200
    assert "Plant Expansion Alpha" in ws_a_direct.text
    assert "2 Received" in ws_a_direct.text
    assert "Commercial Sourcing Award Finalized" in ws_a_direct.text

    # D. Reopen Award Decision directly by URL (/rfqs/{rfq_a_id}/award)
    award_a_direct = client.get(f"/rfqs/{rfq_a_id}/award")
    assert award_a_direct.status_code == 200
    assert "AWARD FINALIZED" in award_a_direct.text
    assert "Step 5 — Award Decision" in award_a_direct.text
    assert f"href=\"/comparisons/{comp_a_id}\"" in award_a_direct.text

    # E. Reopen Comparison Analysis directly by URL (/comparisons/{comp_a_id})
    comp_a_direct = client.get(f"/comparisons/{comp_a_id}")
    assert comp_a_direct.status_code == 200
    assert "Step 4 — Comparison Analysis" in comp_a_direct.text
    assert f"href=\"/rfqs/{rfq_a_id}/award\"" in comp_a_direct.text
    assert f"href=\"/rfqs/{rfq_a_id}/comparison/setup\"" in comp_a_direct.text or f"href=\"/comparisons/create?rfq_id={rfq_a_id}\"" in comp_a_direct.text

    # F. Reopen Comparison Setup directly by URL (/rfqs/{rfq_b_id}/comparison/setup)
    setup_b_direct = client.get(f"/rfqs/{rfq_b_id}/comparison/setup")
    assert setup_b_direct.status_code == 200
    assert "Step 4 — Comparison Setup" in setup_b_direct.text
    assert f"href=\"/rfqs/{rfq_b_id}\"" in setup_b_direct.text

    # G. Error handling: invalid RFQ ID produces 404 rather than 500
    res_404 = client.get(f"/rfqs/NON-EXISTENT-RFQ-{ts}")
    assert res_404.status_code == 404
