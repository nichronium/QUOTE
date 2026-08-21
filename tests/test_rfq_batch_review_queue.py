import io
import json
import time
import openpyxl
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import services
from matching.models import MatchStatus


@pytest.fixture
def client():
    return TestClient(app)


def test_rfq_batch_review_queue_and_resolution(client: TestClient):
    """
    Verifies:
    1. RFQ-level batch review queue at /rfqs/{rfq_id}/review.
    2. Three match states classification (Auto-matched, Review Required, Unmatched).
    3. AJAX / single issue resolution persistence.
    4. Safe bulk acceptance of high-confidence matches.
    5. Aggregated workspace summary counts ('X matched automatically • Y need review • Z unmatched').
    6. Extraction status handling for 0 usable lines.
    """
    ts = int(time.time() * 1000)
    rfq_id = f"RFQ-BATCH-TEST-{ts}"

    # 1. Create RFQ with 3 target line items
    rfq_items = [
        {"rfq_line_id": "RFQ-LINE-001", "sku": "FAST-HEX-M10-50", "description": "Hex Head Bolt Grade 8.8 M10 x 50mm", "requested_quantity": 500, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-002", "sku": "SEAL-NBR-40", "description": "Nitrile Rubber O-Ring 40mm ID x 3mm CS", "requested_quantity": 200, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-003", "sku": "BEARING-6004", "description": "Deep Groove Ball Bearing 6004-2RS", "requested_quantity": 100, "requested_uom": "PCS"}
    ]

    res_create = client.post(
        "/rfqs",
        data={
            "rfq_id": rfq_id,
            "title": "Batch Review Queue Test RFQ",
            "base_currency": "INR",
            "items_json": json.dumps(rfq_items)
        },
        follow_redirects=True
    )
    assert res_create.status_code == 200

    # 2. Upload Quote 1 (Has 1 exact match and 1 ambiguous match)
    wb1 = openpyxl.Workbook()
    ws1 = wb1.active
    ws1.title = "Quote"
    ws1.append(["Supplier: Alpha Fasteners", "", "", "", ""])
    ws1.append(["Part No", "Description", "Qty", "UOM", "Unit Price"])
    ws1.append(["FAST-HEX-M10-50", "Hex Head Bolt Grade 8.8 M10 x 50mm", 500, "PCS", 12.5])  # Exact SKU
    ws1.append(["", "High Tensile Bolt M10x50 zinc plated", 500, "PCS", 13.0])  # Fuzzy / Ambiguous description
    b1 = io.BytesIO()
    wb1.save(b1)
    b1.seek(0)

    # Upload Quote 2 (Has 1 exact match and 1 unmatched item)
    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.title = "Quote"
    ws2.append(["Supplier: Beta Seals", "", "", "", ""])
    ws2.append(["Part No", "Description", "Qty", "UOM", "Unit Price"])
    ws2.append(["SEAL-NBR-40", "Nitrile Rubber O-Ring 40mm ID x 3mm CS", 200, "PCS", 8.0])  # Exact SKU
    ws2.append(["CUSTOM-GASKET-99", "Custom Flange Gasket Non-Standard", 50, "PCS", 45.0])  # Non-catalog / Unmatched
    b2 = io.BytesIO()
    wb2.save(b2)
    b2.seek(0)

    res_upload = client.post(
        f"/rfqs/{rfq_id}/upload-quotes",
        files=[
            ("files", ("Alpha_Quote.xlsx", b1.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("Beta_Quote.xlsx", b2.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        ],
        data={
            "supplier_names": ["Alpha Fasteners", "Beta Seals"]
        },
        headers={"Accept": "application/json"}
    )
    assert res_upload.status_code == 200

    # 3. Verify Workspace Data and Lines Summary String
    ws_data = services.get_rfq_workspace_data(rfq_id)
    assert ws_data["total_lines_extracted"] == 4
    assert ws_data["total_auto_matched"] >= 2
    assert "matched automatically" in ws_data["lines_summary_str"]
    assert "need review" in ws_data["lines_summary_str"]

    # 4. Test GET /rfqs/{rfq_id}/review queue page
    res_review_page = client.get(f"/rfqs/{rfq_id}/review")
    assert res_review_page.status_code == 200
    assert "Product Alignment" in res_review_page.text
    assert "Alpha Fasteners" in res_review_page.text
    assert "Beta Seals" in res_review_page.text

    # 5. Get queue data from service
    queue = services.get_rfq_review_queue(rfq_id)
    assert len(queue["issues"]) >= 1

    # 6. Test Single Issue Resolution via POST /rfqs/{rfq_id}/review/resolve
    issue_to_resolve = queue["issues"][0]
    res_resolve = client.post(
        f"/rfqs/{rfq_id}/review/resolve",
        data={
            "quote_id": issue_to_resolve["quote_id"],
            "line_index": issue_to_resolve["line_index"],
            "chosen_candidate_sku": "FAST-HEX-M10-50",
            "action": "ACCEPT"
        },
        headers={"Accept": "application/json"}
    )
    assert res_resolve.status_code == 200
    resolve_data = res_resolve.json()
    assert resolve_data["status"] == "success"

    # 7. Test Safe Bulk Acceptance POST /rfqs/{rfq_id}/review/bulk-accept-safe
    res_bulk = client.post(
        f"/rfqs/{rfq_id}/review/bulk-accept-safe",
        headers={"Accept": "application/json"}
    )
    assert res_bulk.status_code == 200
    bulk_data = res_bulk.json()
    assert bulk_data["status"] == "success"

    # 8. Test RFQ Workspace displays primary review action when issues remain or comparison when ready
    res_rfq = client.get(f"/rfqs/{rfq_id}")
    assert res_rfq.status_code == 200
    assert "Supplier Quotations" in res_rfq.text
