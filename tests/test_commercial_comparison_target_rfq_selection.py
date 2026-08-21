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


def test_comparison_entry_and_rfq_selection_resolution(client: TestClient):
    """
    Verifies:
    1. Active RFQ is automatically preselected as source of truth when entering comparison.
    2. Target RFQ selector is never empty.
    3. Quotes for the active RFQ are loaded automatically.
    4. Switching RFQs loads isolated quotations without state leakage.
    5. Direct execution of comparison setup without re-selection.
    """
    ts = int(time.time() * 1000)
    rfq_id_1 = f"RFQ-COMP-TEST-A-{ts}"
    rfq_id_2 = f"RFQ-COMP-TEST-B-{ts}"

    # 1. Create RFQ A with 2 items
    rfq_items_a = [
        {"rfq_line_id": "RFQ-LINE-001", "sku": "BOLT-M12", "description": "High Tensile Bolt M12", "requested_quantity": 100, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-002", "sku": "NUT-M12", "description": "Hex Nut M12", "requested_quantity": 100, "requested_uom": "PCS"}
    ]
    res_a = client.post("/rfqs", data={"rfq_id": rfq_id_1, "title": "Hardware Round Alpha", "base_currency": "INR", "items_json": json.dumps(rfq_items_a)}, follow_redirects=True)
    assert res_a.status_code == 200

    # 2. Upload Quote to RFQ A
    wb1 = openpyxl.Workbook()
    ws1 = wb1.active
    ws1.append(["Supplier: Alpha Fasteners", "", "", "", ""])
    ws1.append(["Part No", "Description", "Qty", "UOM", "Unit Price"])
    ws1.append(["BOLT-M12", "High Tensile Bolt M12", 100, "PCS", 25.0])
    ws1.append(["NUT-M12", "Hex Nut M12", 100, "PCS", 10.0])
    b1 = io.BytesIO()
    wb1.save(b1)
    b1.seek(0)
    client.post(f"/rfqs/{rfq_id_1}/upload-quotes", files=[("files", ("Alpha_Quote.xlsx", b1.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))], data={"supplier_names": ["Alpha Fasteners"]})

    # 3. Create RFQ B with 1 item
    rfq_items_b = [
        {"rfq_line_id": "RFQ-LINE-001", "sku": "PUMP-VANE-20", "description": "Industrial Hydraulic Vane Pump", "requested_quantity": 5, "requested_uom": "PCS"}
    ]
    res_b = client.post("/rfqs", data={"rfq_id": rfq_id_2, "title": "Hydraulic Round Beta", "base_currency": "INR", "items_json": json.dumps(rfq_items_b)}, follow_redirects=True)
    assert res_b.status_code == 200

    # 4. Upload Quote to RFQ B
    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.append(["Supplier: Beta Hydraulics", "", "", "", ""])
    ws2.append(["Part No", "Description", "Qty", "UOM", "Unit Price"])
    ws2.append(["PUMP-VANE-20", "Industrial Hydraulic Vane Pump", 5, "PCS", 12500.0])
    b2 = io.BytesIO()
    wb2.save(b2)
    b2.seek(0)
    client.post(f"/rfqs/{rfq_id_2}/upload-quotes", files=[("files", ("Beta_Hydraulics.xlsx", b2.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))], data={"supplier_names": ["Beta Hydraulics"]})

    # 5. Access Comparison Setup directly from RFQ A workspace link: /comparisons/create?rfq_id=...
    res_setup_a = client.get(f"/comparisons/create?rfq_id={rfq_id_1}")
    assert res_setup_a.status_code == 200
    assert "Comparison RFQ" in res_setup_a.text
    assert "Hardware Round Alpha" in res_setup_a.text
    assert rfq_id_1 in res_setup_a.text
    assert "Alpha Fasteners" in res_setup_a.text
    assert "Beta Hydraulics" not in res_setup_a.text  # Strict context isolation

    # 6. Access Comparison Setup from RFQ B workspace link: /rfqs/{rfq_id_2}/compare/setup
    res_setup_b = client.get(f"/rfqs/{rfq_id_2}/compare/setup")
    assert res_setup_b.status_code == 200
    assert "Hydraulic Round Beta" in res_setup_b.text
    assert rfq_id_2 in res_setup_b.text
    assert "Beta Hydraulics" in res_setup_b.text
    assert "Alpha Fasteners" not in res_setup_b.text  # Strict context isolation

    # 7. Execute comparison directly from form post
    res_run = client.post(
        "/comparisons",
        data={
            "rfq_id": rfq_id_1,
            "base_currency": "INR",
            "allocation_method": "PROPORTIONAL_LINE_VALUE"
        },
        follow_redirects=False
    )
    assert res_run.status_code == 303
    comp_url = res_run.headers["location"]
    assert "/comparisons/COMP-" in comp_url

    # 8. View generated comparison
    res_comp = client.get(comp_url)
    assert res_comp.status_code == 200
    assert "Commercial Quotation Evaluation" in res_comp.text or "Comparison" in res_comp.text
    assert rfq_id_1 in res_comp.text
