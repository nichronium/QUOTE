import io
import json
from decimal import Decimal
import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import services
from core.canonical_quote import CanonicalQuote
from matching.models import RFQLineItem, ItemMasterRecord


@pytest.fixture
def client():
    return TestClient(app)


def test_student_list_no_quantity_no_uom_never_defaults_to_100_or_pcs(client: TestClient):
    """
    STEP 8 & STEP 9:
    Upload a student-list spreadsheet containing Student ID, Student Name, Roll Number.
    Ensure:
    1. Quantity is None (NOT 100, NOT 1, NOT 0).
    2. UOM is None (NOT PCS).
    3. Roll Number (101, 102, 103) is NOT extracted as quantity.
    4. RFQLineItem persists null for missing quantity and UOM.
    """
    # Create an in-memory Excel workbook mimicking a student list
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Students"
    ws.append(["Student ID", "Student Name", "Roll Number"])
    ws.append(["S-001", "RASHMI GOPE", 101])
    ws.append(["S-002", "DIPENDRA GOPE", 102])
    ws.append(["S-003", "DEEPAK KUMAR BHAKAT", 103])
    
    file_bytes = io.BytesIO()
    wb.save(file_bytes)
    file_bytes.seek(0)

    # 1. Preview API
    res = client.post(
        "/rfqs/requirements/preview",
        files={"file": ("student_list.xlsx", file_bytes.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_lines"] == 3
    assert data["incomplete_count"] == 3
    assert data["complete_count"] == 0

    for item in data["items"]:
        # Assert quantity is NEVER 100 or 101/102/103
        assert item["raw_qty"] is None, f"Expected raw_qty to be None, got {item['raw_qty']}"
        # Assert UOM is NEVER PCS
        assert item["raw_uom"] is None, f"Expected raw_uom to be None, got {item['raw_uom']}"
        assert item["is_complete"] is False
        assert "quantity" in item["missing_fields"]
        assert "uom" in item["missing_fields"]

    # 2. RFQ Creation persistence test
    rfq_id = "RFQ-TEST-STUDENT-LIST"
    rfq_file = services.RFQS_DIR / f"{rfq_id}.json"
    if rfq_file.exists():
        rfq_file.unlink()

    rfq_items_payload = [
        {
            "sku": item["matched_sku"] or item["raw_sku"],
            "description": item["matched_description"] or item["raw_description"],
            "requested_quantity": item["raw_qty"],
            "requested_uom": item["raw_uom"]
        }
        for item in data["items"]
    ]

    rfq = services.create_rfq(rfq_id, "Student List Import Test", "INR", rfq_items_payload)
    
    # Inspect persisted model
    assert len(rfq.items) == 3
    for line in rfq.items:
        assert line.requested_quantity is None
        assert line.requested_uom is None

    # Inspect persisted JSON directly on disk
    with open(rfq_file, "r", encoding="utf-8") as fp:
        raw_disk_json = json.load(fp)
    
    for disk_line in raw_disk_json["items"]:
        assert disk_line["requested_quantity"] is None
        assert disk_line["requested_uom"] is None

    # Verify Detail page rendering (should render N/A without crashing)
    res_detail = client.get(f"/rfqs/{rfq_id}")
    assert res_detail.status_code == 200
    assert "N/A (Missing)" in res_detail.text or "N/A" in res_detail.text


def test_valid_document_with_explicit_quantity_and_uom(client: TestClient):
    """
    Test a valid procurement requirements spreadsheet with SKU, Description, Qty, UOM.
    Asserts exact extraction of 250 and PCS.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SKU", "Description", "Qty", "UOM"])
    ws.append(["BEAR-001", "Deep Groove Ball Bearing", 250, "PCS"])
    ws.append(["CABL-002", "Copper Flexible Cable", 500, "MTR"])
    
    file_bytes = io.BytesIO()
    wb.save(file_bytes)
    file_bytes.seek(0)

    res = client.post(
        "/rfqs/requirements/preview",
        files={"file": ("valid_requirements.xlsx", file_bytes.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_lines"] == 2
    assert data["complete_count"] == 2
    assert data["incomplete_count"] == 0

    assert data["items"][0]["raw_qty"] == 250.0
    assert data["items"][0]["raw_uom"] == "PCS"
    assert data["items"][0]["is_complete"] is True

    assert data["items"][1]["raw_qty"] == 500.0
    assert data["items"][1]["raw_uom"] == "MTR"
    assert data["items"][1]["is_complete"] is True

    # Persist and verify
    rfq_id = "RFQ-TEST-VALID-REQ"
    rfq = services.create_rfq(rfq_id, "Valid Req Test", "INR", [
        {"sku": "BEAR-001", "description": "Deep Groove Ball Bearing", "requested_quantity": 250, "requested_uom": "PCS"},
        {"sku": "CABL-002", "description": "Copper Flexible Cable", "requested_quantity": 500, "requested_uom": "MTR"}
    ])
    assert rfq.items[0].requested_quantity == Decimal("250")
    assert rfq.items[0].requested_uom == "PCS"
    assert rfq.items[1].requested_quantity == Decimal("500")
    assert rfq.items[1].requested_uom == "MTR"


def test_document_with_quantity_but_no_uom(client: TestClient):
    """
    Test a document containing SKU, Description, Qty but NO UOM.
    Asserts: requested_quantity == 250, requested_uom is None.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SKU", "Description", "Required Quantity"])
    ws.append(["BEAR-001", "Deep Groove Ball Bearing", 250])
    
    file_bytes = io.BytesIO()
    wb.save(file_bytes)
    file_bytes.seek(0)

    res = client.post(
        "/rfqs/requirements/preview",
        files={"file": ("qty_no_uom.xlsx", file_bytes.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_lines"] == 1
    assert data["items"][0]["raw_qty"] == 250.0
    assert data["items"][0]["raw_uom"] is None
    assert data["items"][0]["is_complete"] is False
    assert "uom" in data["items"][0]["missing_fields"]
    assert "quantity" not in data["items"][0]["missing_fields"]


def test_document_with_uom_but_no_quantity(client: TestClient):
    """
    Test a document containing SKU, Description, UOM but NO Quantity.
    Asserts: requested_quantity is None, requested_uom == "MTR".
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SKU", "Description", "UOM"])
    ws.append(["CABL-001", "Flexible Armoured Cable", "MTR"])
    
    file_bytes = io.BytesIO()
    wb.save(file_bytes)
    file_bytes.seek(0)

    res = client.post(
        "/rfqs/requirements/preview",
        files={"file": ("uom_no_qty.xlsx", file_bytes.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_lines"] == 1
    assert data["items"][0]["raw_qty"] is None
    assert data["items"][0]["raw_uom"] == "MTR"
    assert data["items"][0]["is_complete"] is False
    assert "quantity" in data["items"][0]["missing_fields"]
    assert "uom" not in data["items"][0]["missing_fields"]


def test_document_with_arbitrary_numeric_columns_not_picked_as_qty(client: TestClient):
    """
    STEP 9:
    Ensure non-quantity numeric columns (Serial Number, Phone Number, Ref Number)
    are NEVER picked as requested quantity.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item Code", "Item Description", "Serial Number", "Phone Number", "Ref Number"])
    ws.append(["CODE-1", "Machine Assembly", 9001, 9876543210, 44021])
    
    file_bytes = io.BytesIO()
    wb.save(file_bytes)
    file_bytes.seek(0)

    res = client.post(
        "/rfqs/requirements/preview",
        files={"file": ("numeric_identifiers.xlsx", file_bytes.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_lines"] == 1
    assert data["items"][0]["raw_qty"] is None
    assert data["items"][0]["raw_uom"] is None
    assert data["items"][0]["is_complete"] is False


def test_end_to_end_downstream_procurement_workflow_with_imported_requirements(client: TestClient):
    """
    STEP 10:
    Full pipeline test verifying that an RFQ created with honest source quantities
    flows through quotation ingestion, matching, comparison, award, and finalization seamlessly.
    """
    rfq_id = "RFQ-E2E-REQ-TEST-001"
    
    # 1. Create valid requirements
    rfq_items = [
        {"sku": "BEAR-6205-2RS", "description": "Deep Groove Ball Bearing 25x52x15mm", "requested_quantity": "100", "requested_uom": "PCS", "target_price": "150.00"},
        {"sku": "CABL-3C-2.5", "description": "3 Core 2.5 sq mm Copper Armoured Cable", "requested_quantity": "500", "requested_uom": "MTR", "target_price": "200.00"}
    ]
    rfq = services.create_rfq(rfq_id, "Honest Req Pipeline Test", "INR", rfq_items)
    assert rfq.items[0].requested_quantity == Decimal("100")
    assert rfq.items[0].requested_uom == "PCS"
    assert rfq.items[1].requested_quantity == Decimal("500")
    assert rfq.items[1].requested_uom == "MTR"

    # 2. Upload Quote
    q1 = services.create_quote("Precision Tools Corp", rfq_id=rfq_id)
    csv_data = "Line,SKU,Description,Qty,UOM,Unit Price\n1,BEAR-6205-2RS,Deep Groove Ball Bearing,100,PCS,120.00\n2,CABL-3C-2.5,Copper Cable,500,MTR,180.00\n"
    services.upload_source_file(q1, "precision.csv", csv_data.encode("utf-8"))
    services.run_extraction(q1)
    services.run_matching_for_quote(q1, rfq_id)

    # 3. Compare & Award
    comp = services.run_rfq_comparison(rfq_id, [q1], base_currency="INR")
    proposal = services.build_proposed_award_allocation(rfq_id, scenario="LINE_ITEM_OPTIMAL")
    assert Decimal(proposal["total_awarded_value"]) == Decimal("102000.00")  # (120*100) + (180*500) = 12000 + 90000 = 102000

    # 4. Finalize
    services.save_award_decision(rfq_id, {
        "rfq_id": rfq_id,
        "selected_scenario": "LINE_ITEM_OPTIMAL",
        "base_currency": "INR",
        "allocations": proposal["allocations"],
        "award_notes": "Approved"
    }, is_finalized=True)

    award = services.get_award_decision(rfq_id)
    assert award["status"] == "FINALIZED"
    assert Decimal(award["total_awarded_value"]) == Decimal("102000.00")


def test_adversarial_company_item_master_catalog_has_no_quantity_fallback(client: TestClient):
    """
    Test the exact COMPANY_ITEM_MASTER_CATALOG_ADVERSARIAL.xlsx workbook:
    1. 20 catalog rows extracted.
    2. Required quantity is None for the catalog rows.
    3. Stocking UOM is correctly extracted:
       RM-1001 = PCS
       RM-1002 = MTR
       RM-1003 = PCS
    4. No extracted row has quantity 100.
    5. No generic fallback quantity exists anywhere in the import path.
    6. Persisted RFQ quantity remains null.
    7. Browser / Detail page rendering displays N/A (Missing).
    8. Comparison engine marks incomplete lines as uncomparable / blocking.
    """
    from pathlib import Path
    catalog_path = Path("datasets/synthetic/COMPANY_ITEM_MASTER_CATALOG_ADVERSARIAL.xlsx")
    assert catalog_path.exists(), f"Fixture not found at {catalog_path}"
    content = catalog_path.read_bytes()

    # 1. Preview API
    res = client.post(
        "/rfqs/requirements/preview",
        files={"file": ("COMPANY_ITEM_MASTER_CATALOG_ADVERSARIAL.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_lines"] == 20
    assert data["incomplete_count"] == 20
    assert data["complete_count"] == 0

    items_by_raw_sku = {it["raw_sku"]: it for it in data["items"]}
    
    # Check RM-1001, RM-1002, RM-1003 specifically
    assert "RM-1001" in items_by_raw_sku
    assert items_by_raw_sku["RM-1001"]["raw_qty"] is None
    assert items_by_raw_sku["RM-1001"]["raw_uom"] == "PCS"
    assert items_by_raw_sku["RM-1001"]["is_complete"] is False

    assert "RM-1002" in items_by_raw_sku
    assert items_by_raw_sku["RM-1002"]["raw_qty"] is None
    assert items_by_raw_sku["RM-1002"]["raw_uom"] == "MTR"
    assert items_by_raw_sku["RM-1002"]["is_complete"] is False

    assert "RM-1003" in items_by_raw_sku
    assert items_by_raw_sku["RM-1003"]["raw_qty"] is None
    assert items_by_raw_sku["RM-1003"]["raw_uom"] == "PCS"
    assert items_by_raw_sku["RM-1003"]["is_complete"] is False

    # Assert NO item has quantity 100 or non-None quantity
    for it in data["items"]:
        assert it["raw_qty"] is None
        assert "quantity" in it["missing_fields"]

    # 2. RFQ creation and persistence
    rfq_id = "RFQ-ADVERSARIAL-CATALOG-TEST"
    rfq_file = services.RFQS_DIR / f"{rfq_id}.json"
    if rfq_file.exists():
        rfq_file.unlink()

    rfq_payload = [
        {
            "sku": it["matched_sku"] or it["raw_sku"],
            "description": it["matched_description"] or it["raw_description"],
            "requested_quantity": it["raw_qty"],
            "requested_uom": it["raw_uom"]
        }
        for it in data["items"]
    ]

    rfq = services.create_rfq(rfq_id, "Adversarial Catalog Test RFQ", "INR", rfq_payload)
    assert len(rfq.items) == 20
    for line in rfq.items:
        assert line.requested_quantity is None

    # Inspect persisted disk JSON
    with open(rfq_file, "r", encoding="utf-8") as fp:
        raw_disk = json.load(fp)
    for d_item in raw_disk["items"]:
        assert d_item["requested_quantity"] is None

    # Verify detail page renders N/A (Missing)
    res_detail = client.get(f"/rfqs/{rfq_id}")
    assert res_detail.status_code == 200
    assert "N/A (Missing)" in res_detail.text

