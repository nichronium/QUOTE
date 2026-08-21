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


def test_multi_file_quotation_upload_and_ingestion(client: TestClient):
    """
    Verifies:
    1. Simultaneous batch upload of multiple quotation files (XLSX, CSV).
    2. Each file is independently extracted, matched, and attached to the RFQ.
    3. Error tolerance: empty/invalid files do not block valid files in the batch.
    4. UI elements for pre-parse review panel and multi-file selection.
    """
    ts = int(time.time() * 1000)
    rfq_id = f"RFQ-MULTI-TEST-{ts}"

    # 1. Create RFQ with 2 items
    rfq_items = [
        {"rfq_line_id": "RFQ-LINE-001", "sku": "BEARING-6205", "description": "Deep Groove Ball Bearing 6205-2RS", "requested_quantity": 100, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-002", "sku": "SEAL-VITON-50", "description": "Viton Rotary Shaft Oil Seal 50x72x8mm", "requested_quantity": 50, "requested_uom": "PCS"}
    ]

    res_create = client.post(
        "/rfqs",
        data={
            "rfq_id": rfq_id,
            "title": "Multi-Vendor Bearings & Seals RFQ",
            "base_currency": "INR",
            "items_json": json.dumps(rfq_items)
        },
        follow_redirects=True
    )
    assert res_create.status_code == 200

    # 2. Prepare 3 valid quotation files (2 XLSX, 1 CSV) and 1 empty file
    # File 1: Apex (XLSX)
    wb1 = openpyxl.Workbook()
    ws1 = wb1.active
    ws1.title = "Quote"
    ws1.append(["Supplier: Apex Industrial Supplies", "", "", "", ""])
    ws1.append(["Part No", "Description", "Qty", "UOM", "Unit Price"])
    ws1.append(["BEARING-6205", "Deep Groove Ball Bearing 6205-2RS", 100, "PCS", 420.0])
    ws1.append(["SEAL-VITON-50", "Viton Rotary Shaft Oil Seal 50x72x8mm", 50, "PCS", 110.0])
    b1 = io.BytesIO()
    wb1.save(b1)
    b1.seek(0)

    # File 2: Bharat (CSV)
    csv_content = (
        "Supplier: Bharat Fasteners & Spares\n"
        "Part No,Description,Qty,UOM,Unit Price\n"
        "BEARING-6205,Deep Groove Ball Bearing 6205-2RS,100,PCS,435.0\n"
        "SEAL-VITON-50,Viton Rotary Shaft Oil Seal 50x72x8mm,50,PCS,115.0\n"
    ).encode("utf-8")

    # File 3: Delta (XLSX)
    wb3 = openpyxl.Workbook()
    ws3 = wb3.active
    ws3.title = "Quote"
    ws3.append(["Supplier: Delta Precision Parts", "", "", "", ""])
    ws3.append(["Part No", "Description", "Qty", "UOM", "Unit Price"])
    ws3.append(["BEARING-6205", "Deep Groove Ball Bearing 6205-2RS", 100, "PCS", 410.0])
    ws3.append(["SEAL-VITON-50", "Viton Rotary Shaft Oil Seal 50x72x8mm", 50, "PCS", 120.0])
    b3 = io.BytesIO()
    wb3.save(b3)
    b3.seek(0)

    # File 4: Corrupt / Empty File
    empty_content = b""

    # 3. Submit multi-file batch upload to /rfqs/{rfq_id}/upload-quotes with JSON Accept header
    files_payload = [
        ("files", ("Apex_Quote.xlsx", b1.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ("files", ("Bharat_Quote.csv", csv_content, "text/csv")),
        ("files", ("Delta_Pricing.xlsx", b3.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ("files", ("Empty_File.xlsx", empty_content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    ]

    res_upload = client.post(
        f"/rfqs/{rfq_id}/upload-quotes",
        files=files_payload,
        data={
            "supplier_names": ["Apex Industrial", "Bharat Fasteners", "Delta Precision", ""]
        },
        headers={"Accept": "application/json"}
    )
    assert res_upload.status_code == 200
    upload_data = res_upload.json()
    assert upload_data["status"] == "success"
    assert upload_data["processed_count"] == 3
    assert upload_data["failed_count"] == 1
    assert upload_data["failed"][0]["filename"] == "Empty_File.xlsx"

    # 4. Verify all 3 valid quotes are attached to the RFQ in services
    quotes = services.get_quotes_for_rfq(rfq_id)
    assert len(quotes) == 3
    supplier_names = {q.get("supplier_name") or q.get("test_name") for q in quotes}
    assert any("Apex" in s for s in supplier_names)
    assert any("Bharat" in s for s in supplier_names)
    assert any("Delta" in s for s in supplier_names)

    for q in quotes:
        assert q["rfq_items_matched"] == 2
        assert q["scope_coverage_str"] == "2 / 2 (100%)"
        assert q["processing_status"] == "READY_FOR_COMPARISON"

    # 5. Verify UI rendering contains multi-file controls
    res_page = client.get(f"/rfqs/{rfq_id}")
    assert res_page.status_code == 200
    assert "Add Supplier Quotations" in res_page.text
    assert "quote-dropzone" in res_page.text
    assert "staged-quotes-panel" in res_page.text
    assert "stagedQuoteFiles" in res_page.text
    assert "clearAllStagedFiles" in res_page.text
    assert "submitStagedQuotations" in res_page.text
