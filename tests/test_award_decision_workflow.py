import json
import time
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import services


@pytest.fixture
def client():
    return TestClient(app)


def test_award_decision_lifecycle_and_validation(client: TestClient):
    rfq_id = f"RFQ-AWD-{int(time.time()*1000)}"
    rfq_items = [
        {"rfq_line_id": "RFQ-LINE-001", "sku": "BEAR-6205-2RS", "description": "Deep Groove Ball Bearing 25x52x15mm", "requested_quantity": 100, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-002", "sku": "VALV-BALL-3IN", "description": "Industrial Flanged Ball Valve 3-inch", "requested_quantity": 20, "requested_uom": "PCS"}
    ]
    client.post(
        "/rfqs",
        data={
            "rfq_id": rfq_id,
            "title": "Award Lifecycle Test RFQ",
            "base_currency": "INR",
            "items_json": json.dumps(rfq_items)
        },
        follow_redirects=True
    )

    quote1_csv = """Part Number,Description,Qty,UOM,Unit Rate,Tax %
SKF-6205-2RS,Deep Groove Ball Bearing 25x52x15mm,100,PCS,150.00,18%
VALV-BALL-3IN,Industrial Flanged Ball Valve 3-inch,20,PCS,1200.00,18%
"""
    client.post(
        f"/rfqs/{rfq_id}/upload-quote",
        data={"supplier_name": "Apex Engineering"},
        files={"file": ("apex.csv", quote1_csv.encode("utf-8"), "text/csv")},
        follow_redirects=True
    )

    quote2_csv = """SKU,Item Description,Quantity,Unit,Price,GST
BEAR-6205-2RS,Deep Groove Ball Bearing,100,PCS,140.00,18%
VALV-BALL-3IN,Ball Valve 3in Flanged,20,PCS,1250.00,18%
"""
    client.post(
        f"/rfqs/{rfq_id}/upload-quote",
        data={"supplier_name": "Beacon Valves"},
        files={"file": ("beacon.csv", quote2_csv.encode("utf-8"), "text/csv")},
        follow_redirects=True
    )

    quotes = services.get_quotes_for_rfq(rfq_id)
    quote_ids = [q["test_id"] for q in quotes]
    client.post(
        "/comparisons",
        data={
            "rfq_id": rfq_id,
            "quote_ids": quote_ids,
            "base_currency": "INR",
            "charge_allocation": "PROPORTIONAL_LINE_VALUE"
        },
        follow_redirects=True
    )

    # 1. GET /rfqs/{rfq_id}/award - Loads proposed allocation from Scenario A
    res = client.get(f"/rfqs/{rfq_id}/award?scenario=SINGLE_SUPPLIER_L1")
    assert res.status_code == 200
    assert "Step 5" in res.text
    assert "Award Decision" in res.text
    assert "Review and finalize which supplier(s) will receive each RFQ line." in res.text
    assert "Commercial Comparison" in res.text
    assert "Recommended allocation from Comparison" in res.text
    assert "Enterprise Award Validation Checks" in res.text

    # 2. GET with Scenario B - Split Sourcing
    res_b = client.get(f"/rfqs/{rfq_id}/award?scenario=LINE_ITEM_OPTIMAL")
    assert res_b.status_code == 200
    assert "Lowest Line Cost (Split)" in res_b.text

    # 3. Finalize Award with full allocations
    award_data = services.build_proposed_award_allocation(rfq_id, scenario="SINGLE_SUPPLIER_L1")
    allocations = award_data["allocations"]

    res_finalize = client.post(
        f"/rfqs/{rfq_id}/award/finalize",
        data={
            "selected_scenario": "SINGLE_SUPPLIER_L1",
            "base_currency": award_data["base_currency"],
            "allocations_json": json.dumps(allocations),
            "buyer_accepted_unallocated": "false"
        },
        follow_redirects=True
    )
    assert res_finalize.status_code == 200
    assert "AWARD FINALIZED" in res_finalize.text

    # 4. Check persisted award record
    record = services.get_award_decision(rfq_id)
    assert record is not None
    assert record["status"] == "FINALIZED"
    assert record["total_required_items"] == len(allocations)
    assert record["validation_passed"] is True

    # 5. Reopen Award
    res_reopen = client.post(
        f"/rfqs/{rfq_id}/award/reopen",
        follow_redirects=True
    )
    assert res_reopen.status_code == 200
    assert "AWARD REOPENED FOR REVISION" in res_reopen.text
    record_reopened = services.get_award_decision(rfq_id)
    assert record_reopened["status"] == "REOPENED"
