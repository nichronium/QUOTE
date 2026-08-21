import json
import time
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import services


@pytest.fixture
def client():
    return TestClient(app)


def test_complete_user_rfq_comparison_award_journey(client: TestClient):
    rfq_id = f"RFQ-JOURNEY-{int(time.time()*1000)}"
    rfq_items = [
        {"rfq_line_id": "RFQ-LINE-001", "sku": "BEAR-6205-2RS", "description": "Deep Groove Ball Bearing 25x52x15mm", "requested_quantity": 100, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-002", "sku": "VALV-BALL-3IN", "description": "Industrial Flanged Ball Valve 3-inch", "requested_quantity": 20, "requested_uom": "PCS"}
    ]
    res_create_rfq = client.post(
        "/rfqs",
        data={
            "rfq_id": rfq_id,
            "title": "Fresh Plant Spares Procurement",
            "base_currency": "INR",
            "items_json": json.dumps(rfq_items)
        },
        follow_redirects=True
    )
    assert res_create_rfq.status_code == 200
    assert rfq_id in res_create_rfq.text

    # 2. Ingest Quote 1 (Apex)
    quote1_csv = """Part Number,Description,Qty,UOM,Unit Rate,Tax %
SKF-6205-2RS,Deep Groove Ball Bearing 25x52x15mm,100,PCS,150.00,18%
VALV-BALL-3IN,Industrial Flanged Ball Valve 3-inch,20,PCS,1200.00,18%
"""
    res_up1 = client.post(
        f"/rfqs/{rfq_id}/upload-quote",
        data={"supplier_name": "Apex Engineering Supplies"},
        files={"file": ("apex_quote.csv", quote1_csv.encode("utf-8"), "text/csv")},
        follow_redirects=True
    )
    assert res_up1.status_code == 200

    # 3. Ingest Quote 2 (Beacon)
    quote2_csv = """SKU,Item Description,Quantity,Unit,Price,GST
BEAR-6205-2RS,Deep Groove Ball Bearing,100,PCS,140.00,18%
VALV-BALL-3IN,Ball Valve 3in Flanged,20,PCS,1250.00,18%
"""
    res_up2 = client.post(
        f"/rfqs/{rfq_id}/upload-quote",
        data={"supplier_name": "Beacon Valves Ltd"},
        files={"file": ("beacon_quote.csv", quote2_csv.encode("utf-8"), "text/csv")},
        follow_redirects=True
    )
    assert res_up2.status_code == 200

    # 4. Check RFQ Workspace: verify 2 quotes ready, zero errors
    res_ws = client.get(f"/rfqs/{rfq_id}")
    assert res_ws.status_code == 200
    assert "Apex Engineering Supplies" in res_ws.text
    assert "Beacon Valves Ltd" in res_ws.text
    assert "Compare 2 Eligible Supplier Quotations" in res_ws.text or "Run Commercial Comparison" in res_ws.text

    # 5. Open Comparison Setup: check 1-step back navigation
    res_comp_setup = client.get(f"/comparisons/create?rfq_id={rfq_id}")
    assert res_comp_setup.status_code == 200
    assert f'href="/rfqs/{rfq_id}"' in res_comp_setup.text
    assert "Back to RFQ Workspace" in res_comp_setup.text

    # 6. Execute Comparison
    quotes = services.get_quotes_for_rfq(rfq_id)
    quote_ids = [q["test_id"] for q in quotes]
    assert len(quote_ids) == 2

    res_run_comp = client.post(
        "/comparisons",
        data={
            "rfq_id": rfq_id,
            "quote_ids": quote_ids,
            "base_currency": "INR",
            "charge_allocation": "PROPORTIONAL_LINE_VALUE"
        },
        follow_redirects=False
    )
    assert res_run_comp.status_code == 303
    comp_url = res_run_comp.headers["location"]
    comp_id = comp_url.split("/")[-1]

    # 7. View Comparison Analysis: check 1-step back navigation & award forward CTA
    res_comp_analysis = client.get(f"/comparisons/{comp_id}")
    assert res_comp_analysis.status_code == 200
    assert f'href="/comparisons/create?rfq_id={rfq_id}"' in res_comp_analysis.text
    assert "Back to Comparison Setup" in res_comp_analysis.text
    assert f'href="/rfqs/{rfq_id}/award"' in res_comp_analysis.text
    assert "Proceed to Award Decision" in res_comp_analysis.text

    # 8. Open Award Decision: check 1-step back navigation
    res_award_page = client.get(f"/rfqs/{rfq_id}/award?scenario=SINGLE_SUPPLIER_L1")
    assert res_award_page.status_code == 200
    assert f'href="/comparisons/{comp_id}"' in res_award_page.text
    assert "Back to Comparison" in res_award_page.text
    assert "Step 5 — Award Decision" in res_award_page.text

    # 9. Finalize Award
    proposed_award = services.build_proposed_award_allocation(rfq_id, scenario="SINGLE_SUPPLIER_L1")
    allocations = proposed_award["allocations"]

    res_finalize = client.post(
        f"/rfqs/{rfq_id}/award/finalize",
        data={
            "selected_scenario": "SINGLE_SUPPLIER_L1",
            "base_currency": "INR",
            "allocations_json": json.dumps(allocations),
            "buyer_accepted_unallocated": "false"
        },
        follow_redirects=True
    )
    assert res_finalize.status_code == 200
    assert "AWARD FINALIZED" in res_finalize.text

    # 10. Reload RFQ Workspace: verify Finalized Award state is persisted
    res_ws_final = client.get(f"/rfqs/{rfq_id}")
    assert res_ws_final.status_code == 200
    assert "Commercial Sourcing Award Finalized" in res_ws_final.text
    assert "View Finalized Award Record" in res_ws_final.text
