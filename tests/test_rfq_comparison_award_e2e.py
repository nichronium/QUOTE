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


def test_user_created_rfq_linear_workflow_navigation_and_award(client: TestClient):
    """
    Verifies complete linear navigation, unallocated confirmation gate, and zero internal errors across:
    1. RFQ Workspace
    2. Comparison Setup
    3. Comparison Analysis
    4. Award Decision
    5. Award Finalization & Persistence
    """
    # 1. Create a fresh user RFQ with catalog items
    rfq_id = f"RFQ-E2E-{int(time.time()*1000)}"
    items_payload = [
        {
            "internal_item_id": "ITEM-001",
            "sku": "BEAR-6205-2RS",
            "description": "Deep Groove Ball Bearing 25x52x15mm Rubber Sealed",
            "requested_quantity": 100.0,
            "requested_uom": "PCS"
        },
        {
            "internal_item_id": "ITEM-004",
            "sku": "FAST-HEX-M8-40-SS",
            "description": "Hex Head Bolt M8 x 40mm SS304",
            "requested_quantity": 200.0,
            "requested_uom": "PCS"
        }
    ]

    create_res = client.post(
        "/rfqs",
        data={
            "rfq_id": rfq_id,
            "title": "E2E Linear Workflow Test RFQ",
            "base_currency": "INR",
            "items_json": json.dumps(items_payload)
        },
        follow_redirects=True
    )
    assert create_res.status_code == 200
    assert "E2E Linear Workflow Test RFQ" in create_res.text

    # 2. Test RFQ Workspace when 0 quotes exist
    rfq_res = client.get(f"/rfqs/{rfq_id}")
    assert rfq_res.status_code == 200
    assert "0 Received" in rfq_res.text
    assert "&larr; Back to RFQs" in rfq_res.text

    # Clicking Comparison shortcut with 0 quotes -> redirects cleanly to RFQ or create setup
    comp_shortcut_res = client.get(f"/rfqs/{rfq_id}/comparison", follow_redirects=True)
    assert comp_shortcut_res.status_code == 200

    # Clicking Award shortcut with 0 quotes -> redirects cleanly without error
    award_shortcut_res = client.get(f"/rfqs/{rfq_id}/award", follow_redirects=True)
    assert award_shortcut_res.status_code == 200

    # 3. Ingest test quotes for this RFQ (Quote A and Quote B)
    # Quote A: Apex Industrial
    wb_a = openpyxl.Workbook()
    ws_a = wb_a.active
    ws_a.title = "Quotation"
    ws_a.append(["Apex Industrial Supplies Ltd"])
    ws_a.append(["Quotation No:", "QTN-APEX-001", "Date:", "2026-08-20"])
    ws_a.append(["Currency:", "INR", "Payment Terms:", "Net 30 Days"])
    ws_a.append([])
    ws_a.append(["Supplier SKU", "Description", "Qty", "UOM", "Unit Price", "Tax %"])
    ws_a.append(["SKF-6205-2RS", "Deep Groove Ball Bearing 25x52x15mm", 100, "PCS", 450.00, 18.0])
    buf_a = io.BytesIO()
    wb_a.save(buf_a)

    res_upload_a = client.post(
        f"/rfqs/{rfq_id}/upload",
        files={"file": ("supplier_apex.xlsx", buf_a.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"supplier_name": "Apex Industrial Supplies Ltd"},
        follow_redirects=True
    )
    assert res_upload_a.status_code == 200
    assert "Apex Industrial Supplies Ltd" in res_upload_a.text

    # Quote B: Beta Dynamics (Quotes 1 item: Bearing only - partial quote)
    wb_b = openpyxl.Workbook()
    ws_b = wb_b.active
    ws_b.title = "Quotation"
    ws_b.append(["Beta Dynamics Corp"])
    ws_b.append(["Quotation No:", "QTN-BETA-001", "Date:", "2026-08-20"])
    ws_b.append(["Currency:", "INR", "Payment Terms:", "Net 30 Days"])
    ws_b.append([])
    ws_b.append(["Supplier SKU", "Description", "Qty", "UOM", "Unit Price", "Tax %"])
    ws_b.append(["SKF-6205-2RS", "Deep Groove Ball Bearing 25x52x15mm", 100, "PCS", 430.00, 18.0])
    buf_b = io.BytesIO()
    wb_b.save(buf_b)

    res_upload_b = client.post(
        f"/rfqs/{rfq_id}/upload",
        files={"file": ("supplier_beta.xlsx", buf_b.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"supplier_name": "Beta Dynamics Corp"},
        follow_redirects=True
    )
    assert res_upload_b.status_code == 200
    assert "Beta Dynamics Corp" in res_upload_b.text

    # Get quote IDs
    quotes = services.get_quotes_for_rfq(rfq_id)
    assert len(quotes) == 2
    quote_ids = [q["test_id"] for q in quotes]

    # Run matching for both
    for qid in quote_ids:
        client.post(f"/quotes/{qid}/match/execute", data={"rfq_id": rfq_id}, follow_redirects=True)

    # 4. Open Comparison Setup (/comparisons/create?rfq_id={rfq_id})
    setup_res = client.get(f"/comparisons/create?rfq_id={rfq_id}")
    assert setup_res.status_code == 200
    assert "Step 4 — Comparison Setup" in setup_res.text
    # Exactly one step back to RFQ Workspace
    assert f"href=\"/rfqs/{rfq_id}\"" in setup_res.text
    assert "Back to RFQ Workspace" in setup_res.text

    # 5. Run Comparison on /comparisons
    run_comp_res = client.post(
        "/comparisons",
        data={
            "rfq_id": rfq_id,
            "quote_ids": quote_ids,
            "base_currency": "INR",
            "charge_allocation": "PROPORTIONAL_LINE_VALUE"
        },
        follow_redirects=False
    )
    assert run_comp_res.status_code == 303
    comp_redirect_url = run_comp_res.headers["location"]
    comp_id = comp_redirect_url.split("/")[-1]

    # 6. Open Comparison Analysis (/comparisons/{comp_id})
    comp_detail_res = client.get(f"/comparisons/{comp_id}")
    assert comp_detail_res.status_code == 200
    assert "Step 4 — Comparison Analysis" in comp_detail_res.text
    # Exactly one step back to Comparison Setup
    assert f"href=\"/comparisons/create?rfq_id={rfq_id}\"" in comp_detail_res.text
    assert "Back to Comparison Setup" in comp_detail_res.text
    # Clear CTA to Award Decision
    assert f"href=\"/rfqs/{rfq_id}/award\"" in comp_detail_res.text
    assert "Proceed to Award Decision" in comp_detail_res.text

    # 7. Open Award Decision (/rfqs/{rfq_id}/award)
    award_res = client.get(f"/rfqs/{rfq_id}/award")
    assert award_res.status_code == 200
    assert "Step 5 — Award Decision" in award_res.text
    # Exactly one step back to Comparison Analysis
    assert f"href=\"/comparisons/{comp_id}\"" in award_res.text
    assert "Back to Comparison Analysis" in award_res.text

    # 8. Test Partial Award Guardrail (Unallocated Item requires confirmation)
    award_data = services.build_proposed_award_allocation(rfq_id, comp_id, "SINGLE_SUPPLIER_L1")
    allocations = award_data["allocations"]

    # 8a. Attempt finalize without buyer confirmation -> blocked
    finalize_blocked = client.post(
        f"/rfqs/{rfq_id}/award/finalize",
        data={
            "selected_scenario": "SINGLE_SUPPLIER_L1",
            "base_currency": "INR",
            "allocations_json": json.dumps(allocations),
            "buyer_accepted_unallocated": "false"
        },
        follow_redirects=True
    )
    assert finalize_blocked.status_code == 200
    award_rec_draft = services.get_award_decision(rfq_id)
    assert award_rec_draft["status"] == "DRAFT"

    # 8b. Finalize with explicit buyer confirmation checkbox
    finalize_confirmed = client.post(
        f"/rfqs/{rfq_id}/award/finalize",
        data={
            "selected_scenario": "SINGLE_SUPPLIER_L1",
            "base_currency": "INR",
            "allocations_json": json.dumps(allocations),
            "buyer_accepted_unallocated": "true"
        },
        follow_redirects=True
    )
    assert finalize_confirmed.status_code == 200
    assert "AWARD FINALIZED" in finalize_confirmed.text
    # Back button remains linear to Comparison Analysis
    assert f"href=\"/comparisons/{comp_id}\"" in finalize_confirmed.text

    # 9. Verify Award record is persisted and present on RFQ Workspace reload
    award_rec = services.get_award_decision(rfq_id)
    assert award_rec is not None
    assert award_rec["status"] == "FINALIZED"
    assert award_rec["validation_passed"] is True

    rfq_reloaded = client.get(f"/rfqs/{rfq_id}")
    assert rfq_reloaded.status_code == 200
    assert "STAGE 5 &bull; AWARD FINALIZED" in rfq_reloaded.text
    assert "Commercial Sourcing Award Finalized" in rfq_reloaded.text
    assert f"href=\"/rfqs/{rfq_id}/award\"" in rfq_reloaded.text
    assert "View Finalized Award Record" in rfq_reloaded.text
