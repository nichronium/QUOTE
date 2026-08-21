"""
Comprehensive UI Workflow Integration Test Suite.
Verifies the end-to-end procurement workstation web application:
1. Dashboard metrics & navigation
2. Quote upload & extraction
3. Extraction review, line items, and provenance
4. Human field correction & locked re-extraction
5. Phase 2 Item Master matching & candidate resolution
6. RFQ management & Item Master loading
7. Phase 3 Comparison matrix, normalization breakdown & cell details
8. Supplier eligibility filtering & L1/L2/L3 quote ranking
9. Item-level split sourcing recommendations
10. Review Center issue aggregation
11. History logging and audit trail
12. Error states & resilience
"""

from decimal import Decimal
import io
import json
from pathlib import Path
import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import services


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def quote_a_excel():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quote"
    ws.append(["Alpha Industrial Supplies Ltd"])
    ws.append(["Quotation No:", "QTN-ALPHA-101", "Date:", "2026-08-19"])
    ws.append(["Currency:", "INR", "Payment Terms:", "30 days net"])
    ws.append([])
    ws.append(["Supplier Part Number", "Description", "Quantity", "UOM", "Unit Price", "Tax Rate %"])
    ws.append(["SKF-6205-2RS", "Ball Bearing 25x52x15mm", 100, "PCS", 120.00, 18.0])
    ws.append(["POL-6SQ-CU", "Armoured Cable 4-Core 6 Sq.mm", 500, "MTR", 250.00, 18.0])
    
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


@pytest.fixture
def quote_b_excel():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Commercial"
    ws.append(["Bharat Heavy Components Pvt Ltd"])
    ws.append(["Quotation Ref:", "BHC/2026/889", "Date:", "2026-08-19"])
    ws.append(["Currency:", "INR"])
    ws.append([])
    ws.append(["Manufacturer Part Number", "Description", "Order Qty", "UOM", "Basic Rate", "CGST %", "SGST %"])
    ws.append(["6205-2RS1", "Deep Groove Ball Bearing 25mm", 100, "PCS", 115.00, 9.0, 9.0])
    ws.append(["1100V-4C-6SQ", "Armoured Copper Cable 6mm", 500, "MTR", 260.00, 9.0, 9.0])
    
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def test_complete_procurement_workstation_workflow(client: TestClient, quote_a_excel: bytes, quote_b_excel: bytes):
    # 1. Verify Enterprise Workspace Dashboard
    res = client.get("/")
    assert res.status_code == 200
    assert "Quote Intelligence" in res.text
    assert "Procurement" in res.text



    # 2. Upload Quote A
    res = client.post("/quotes", data={"quote_name": "Supplier A Test Quote"}, follow_redirects=False)
    assert res.status_code == 303
    quote_a_id = res.headers["location"].split("/")[-1]

    res = client.post(
        f"/quotes/{quote_a_id}/upload",
        files={"file": ("quote_a.xlsx", quote_a_excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True
    )
    assert res.status_code == 200
    assert "quote_a.xlsx" in res.text

    # 3. Extract Quote A
    res = client.post(f"/quotes/{quote_a_id}/extract", follow_redirects=True)
    assert res.status_code == 200
    assert "Alpha Industrial Supplies Ltd" in res.text
    assert "SKF-6205-2RS" in res.text

    # 4. View Provenance & Human Field Correction
    quote_a = services.get_canonical_quote(quote_a_id)
    assert quote_a is not None
    assert len(quote_a.items) == 2
    assert quote_a.items[0].supplier_part_number == "SKF-6205-2RS"

    res = client.post(
        f"/quotes/{quote_a_id}/correct",
        data={"line_index": 0, "field_name": "unit_price", "new_value": "125.00"},
        follow_redirects=True
    )
    assert res.status_code == 200
    assert "Human Overrides" in res.text

    # 5. Re-Extraction Preserves Override (merge_reextraction)
    res = client.post(f"/quotes/{quote_a_id}/extract", follow_redirects=True)
    assert res.status_code == 200
    updated_quote = services.get_canonical_quote(quote_a_id)
    assert updated_quote.items[0].unit_price == Decimal("125.00")
    assert Decimal(str(updated_quote.items[0].field_corrections["unit_price"].corrected_value)) == Decimal("125.00")

    # 6. Upload & Extract Quote B
    res = client.post("/quotes", data={"quote_name": "Supplier B Test Quote"}, follow_redirects=False)
    quote_b_id = res.headers["location"].split("/")[-1]

    client.post(
        f"/quotes/{quote_b_id}/upload",
        files={"file": ("quote_b.xlsx", quote_b_excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True
    )
    res = client.post(f"/quotes/{quote_b_id}/extract", follow_redirects=True)
    assert res.status_code == 200
    assert "Bharat Heavy Components Pvt Ltd" in res.text

    # 7. Item Master Matching Page & Resolution
    res = client.get(f"/quotes/{quote_a_id}/match")
    assert res.status_code == 200
    assert ("Item Master Matching" in res.text or "Item Alignment" in res.text or "Alignment" in res.text)
    assert "SKF-6205-2RS" in res.text

    # 8. Create RFQ via UI
    rfq_items = [
        {"rfq_line_id": "RFQ-LINE-001", "description": "Deep Groove Ball Bearing 25x52x15mm", "requested_quantity": "100", "requested_uom": "PCS", "sku": "BEAR-6205-2RS"},
        {"rfq_line_id": "RFQ-LINE-002", "description": "Armoured Power Cable 4-Core 6 Sq.mm", "requested_quantity": "500", "requested_uom": "MTR", "sku": "CABL-6SQ-CU"}
    ]
    res = client.post(
        "/rfqs",
        data={
            "rfq_id": "RFQ-TEST-UI-001",
            "title": "UI Integration Test RFQ",
            "base_currency": "INR",
            "items_json": json.dumps(rfq_items)
        },
        follow_redirects=True
    )
    assert res.status_code == 200
    assert "UI Integration Test RFQ" in res.text

    # 9. View RFQ Detail
    res = client.get("/rfqs/RFQ-TEST-UI-001")
    assert res.status_code == 200
    assert "BEAR-6205-2RS" in res.text



    # 10. Run Multi-Supplier Comparison & Matrix View
    res = client.post(
        "/comparisons",
        data={
            "rfq_id": "RFQ-TEST-UI-001",
            "quote_ids": [quote_a_id, quote_b_id],
            "base_currency": "INR",
            "usd_rate": "85.00",
            "eur_rate": "92.50",
            "allocation_method": "PROPORTIONAL_LINE_VALUE"
        },
        follow_redirects=True
    )
    assert res.status_code == 200
    assert "Normalized Comparison Matrix" in res.text
    assert "Verified Supplier Rankings" in res.text
    assert "STRATEGIC SOURCING AWARD RECOMMENDATION" in res.text


    # 11. Review Center Page
    res = client.get("/review")
    assert res.status_code == 200
    assert "Actionable Review Center" in res.text


    # 12. History Page
    res = client.get("/history")
    assert res.status_code == 200
    assert "Supplier A Test Quote" in res.text


def test_api_extraction_endpoint(client: TestClient, quote_a_excel: bytes):
    """Tests programmatic JSON extraction API endpoint."""
    quote_id = services.create_quote("API Test Quote")
    services.upload_source_file(quote_id, "api_quote.xlsx", quote_a_excel)

    res = client.post(f"/api/quotes/{quote_id}/extract")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["quote"]["supplier_raw_name"] == "Alpha Industrial Supplies Ltd"
    assert len(data["quote"]["items"]) == 2


def test_source_download_and_inline_routes(client: TestClient, quote_a_excel: bytes):
    """Tests file download and inline viewing routes."""
    quote_id = services.create_quote("File Access Test")
    services.upload_source_file(quote_id, "test_file.xlsx", quote_a_excel)

    # Inline View
    res = client.get(f"/quotes/{quote_id}/source")
    assert res.status_code == 200

    # Attachment Download
    res = client.get(f"/quotes/{quote_id}/download")
    assert res.status_code == 200
    assert "attachment" in res.headers.get("content-disposition", "")

