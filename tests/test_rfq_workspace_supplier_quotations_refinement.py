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


def test_rfq_workspace_coverage_presentation_and_status_model(client: TestClient):
    """
    Verifies:
    1. RFQ scope is the denominator in coverage: matched RFQ items / total RFQ items (%).
    2. Dynamic breakdown strings: 'N supplier lines extracted · M matched to RFQ · X unmatched / extra'.
    3. Status model: Received -> Extracted -> Review Needed -> Ready for Comparison.
    4. Top summary: 'N Quotes Received · X Ready for Comparison · Y Need Review'.
    5. Partial quotes participate in comparison seamlessly.
    """
    ts = int(time.time() * 1000)
    rfq_id = f"RFQ-WORKSPACE-TEST-{ts}"

    # 1. Create an RFQ with 4 line items
    rfq_items = [
        {"rfq_line_id": "RFQ-LINE-001", "sku": "BEARING-6205", "description": "Deep Groove Ball Bearing 6205-2RS", "requested_quantity": 100, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-002", "sku": "SEAL-VITON-50", "description": "Viton Rotary Shaft Oil Seal 50x72x8mm", "requested_quantity": 50, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-003", "sku": "BOLT-M12-50", "description": "Hex Head Bolt SS304 M12x50mm", "requested_quantity": 200, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-004", "sku": "NUT-M12", "description": "Hex Nut SS304 M12 DIN 934", "requested_quantity": 200, "requested_uom": "PCS"}
    ]

    res_create = client.post(
        "/rfqs",
        data={
            "rfq_id": rfq_id,
            "title": "Industrial Maintenance Package",
            "base_currency": "INR",
            "items_json": json.dumps(rfq_items)
        },
        follow_redirects=True
    )
    assert res_create.status_code == 200

    # 2. Upload Quote 1: Partial Quote (Quotes 2 of 4 items + 1 extra line = 3 extracted lines)
    wb1 = openpyxl.Workbook()
    ws1 = wb1.active
    ws1.title = "Quote"
    ws1.append(["Supplier: Partial Bearings Ltd", "", "", "", ""])
    ws1.append(["SKU", "Description", "Qty", "UOM", "Unit Price"])
    ws1.append(["BEARING-6205", "Deep Groove Ball Bearing 6205-2RS", 100, "PCS", 450.0])
    ws1.append(["SEAL-VITON-50", "Viton Rotary Shaft Oil Seal 50x72x8mm", 50, "PCS", 120.0])
    ws1.append(["EXTRA-GREASE", "High Temp Bearing Grease 500g", 10, "CAN", 350.0])

    b1 = io.BytesIO()
    wb1.save(b1)
    b1.seek(0)

    res_up1 = client.post(
        f"/rfqs/{rfq_id}/upload-quote",
        data={"supplier_name": "Partial Bearings Ltd"},
        files={"file": ("quote_partial.xlsx", b1.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True
    )
    assert res_up1.status_code == 200

    # 3. Upload Quote 2: Complete Quote (Quotes all 4 items)
    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.title = "Quote"
    ws2.append(["Supplier: Full Industrial Corp", "", "", "", ""])
    ws2.append(["SKU", "Description", "Qty", "UOM", "Unit Price"])
    ws2.append(["BEARING-6205", "Deep Groove Ball Bearing 6205-2RS", 100, "PCS", 440.0])
    ws2.append(["SEAL-VITON-50", "Viton Rotary Shaft Oil Seal 50x72x8mm", 50, "PCS", 125.0])
    ws2.append(["BOLT-M12-50", "Hex Head Bolt SS304 M12x50mm", 200, "PCS", 18.0])
    ws2.append(["NUT-M12", "Hex Nut SS304 M12 DIN 934", 200, "PCS", 5.0])

    b2 = io.BytesIO()
    wb2.save(b2)
    b2.seek(0)

    res_up2 = client.post(
        f"/rfqs/{rfq_id}/upload-quote",
        data={"supplier_name": "Full Industrial Corp"},
        files={"file": ("quote_full.xlsx", b2.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True
    )
    assert res_up2.status_code == 200

    # 4. Extract both quotes
    quotes = services.get_quotes_for_rfq(rfq_id)
    assert len(quotes) == 2

    for q in quotes:
        qid = q["quote_id"]
        client.post(f"/quotes/{qid}/extract", follow_redirects=True)

    # 5. Verify Workspace Data
    ws_data = services.get_rfq_workspace_data(rfq_id)
    assert ws_data["total_quotes_count"] == 2
    assert ws_data["ready_count"] == 2
    assert ws_data["need_review_count"] == 0
    assert "2 Quotes Received · 2 Ready for Comparison · 0 Need Review" in ws_data["quotes_summary_str"]

    q_partial = next(q for q in ws_data["quotes"] if "Partial" in (q.get("supplier_name") or ""))
    assert q_partial["rfq_items_matched"] == 2
    assert q_partial["total_rfq_items"] == 4
    assert q_partial["lines_extracted_count"] == 3
    assert q_partial["extra_lines_count"] == 1
    assert q_partial["scope_coverage_str"] == "2 / 4 (50%)"
    assert q_partial["processing_status"] == "READY_FOR_COMPARISON"

    # 6. Verify HTML Rendering
    res_page = client.get(f"/rfqs/{rfq_id}")
    assert res_page.status_code == 200
    assert "2 / 4 (50%)" in res_page.text
    assert "3 supplier lines extracted" in res_page.text
    assert "2 matched to RFQ" in res_page.text
    assert "1 unmatched / extra" in res_page.text
    assert "Ready for Comparison" in res_page.text
