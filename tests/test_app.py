from decimal import Decimal
import io
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
def sample_excel_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Apex Precision Tools Ltd"])
    ws.append(["Quotation Ref:", "QT-APEX-998", "Date:", "2026-08-19"])
    ws.append(["Payment Terms:", "30 Days Credit"])
    ws.append([])
    ws.append(["Item Code", "Description", "HSN", "Qty", "UOM", "Rate", "Tax %"])
    ws.append(["ENDMILL-06", "Solid Carbide Endmill 6mm 4F", "8207", 10, "Nos", 450.00, 18.0])
    ws.append(["DRILL-08", "HSS Drill Bit 8.5mm", "8207", 20, "PCS", 85.00, 18.0])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def test_full_test_harness_lifecycle(client: TestClient, sample_excel_bytes: bytes):
    """
    Verifies full lifecycle in Extraction Lab:
    Create Test -> Upload -> Process -> Correct Field -> Re-run (merge) -> Reset -> Delete
    """
    # 1. Create Test
    res = client.post("/tests", data={"test_name": "Apex Tools Extraction Test"}, follow_redirects=False)
    assert res.status_code == 303
    redirect_url = res.headers["location"]
    test_id = redirect_url.split("/")[-1]
    assert test_id.startswith("TEST-")

    # 2. View Test page
    res = client.get(f"/tests/{test_id}")
    assert res.status_code == 200
    assert "Apex Tools Extraction Test" in res.text

    # 3. Upload Source File
    res = client.post(
        f"/tests/{test_id}/upload",
        files={"file": ("apex_quote.xlsx", sample_excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True
    )
    assert res.status_code == 200
    assert "apex_quote.xlsx" in res.text

    # 4. Process Extraction
    res = client.post(f"/tests/{test_id}/process", follow_redirects=True)
    assert res.status_code == 200
    assert "Solid Carbide Endmill" in res.text

    # Verify JSON API endpoint
    json_res = client.get(f"/tests/{test_id}/json")
    assert json_res.status_code == 200
    data = json_res.json()
    assert data["quote_number"] == "QT-APEX-998"
    assert len(data["items"]) == 2
    assert Decimal(str(data["items"][0]["unit_price"])) == Decimal("450")

    # 5. Apply Human Field Correction
    res = client.post(
        f"/tests/{test_id}/correct",
        data={
            "line_index": 0,
            "field_name": "unit_price",
            "new_value": "425.00"
        },
        follow_redirects=True
    )
    assert res.status_code == 200

    # Verify updated price in JSON
    json_res = client.get(f"/tests/{test_id}/json")
    data = json_res.json()
    assert Decimal(str(data["items"][0]["unit_price"])) == Decimal("425")
    assert "unit_price" in data["items"][0]["field_corrections"]

    # 6. Re-run Extraction (Verifying Human Override is Preserved)
    res = client.post(f"/tests/{test_id}/process", follow_redirects=True)
    assert res.status_code == 200
    json_res = client.get(f"/tests/{test_id}/json")
    data = json_res.json()
    # Corrected price should stay 425.00 even after re-extracting the original file!
    assert Decimal(str(data["items"][0]["unit_price"])) == Decimal("425")
    assert "unit_price" in data["items"][0]["field_corrections"]

    # 7. Reset Test
    res = client.post(f"/tests/{test_id}/reset", follow_redirects=True)
    assert res.status_code == 200
    test_data = services.get_test_details(test_id)
    assert test_data["canonical_quote"] is None
    assert test_data["has_source"] is True

    # 8. Check History Endpoint
    res = client.get("/history")
    assert res.status_code == 200
    assert test_id in res.text

    # 9. Clean up / Delete Test
    res = client.post(f"/tests/{test_id}/delete", follow_redirects=True)
    assert res.status_code == 200
    assert services.get_test_details(test_id) is None
