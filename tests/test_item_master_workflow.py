import io
import json
import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import services


@pytest.fixture
def client():
    return TestClient(app)


def test_item_master_page_displays_company_catalog(client: TestClient):
    """Verifies that Item Master clearly shows company catalog with counts and entry actions."""
    res = client.get("/item-master")
    assert res.status_code == 200
    assert "Company Product Master" in res.text
    assert "Import Catalog" in res.text
    assert "Add Product Manually" in res.text
    assert "Products in Catalog" in res.text
    assert "BEAR-6205-2RS" in res.text


def test_add_product_manually_and_view_detail(client: TestClient):
    """Verifies manual product entry and detail view."""
    # 1. View form
    res = client.get("/item-master/new")
    assert res.status_code == 200
    assert "Add Product to Company Catalog" in res.text

    # 2. Submit new product
    sku = "TEST-PUMP-500"
    res = client.post(
        "/item-master/new",
        data={
            "internal_sku": sku,
            "canonical_description": "Centrifugal Industrial Water Pump 5HP 3-Phase",
            "stocking_uom": "NOS",
            "manufacturer_part_number": "CP-5HP-3P-415V",
            "brand": "Kirloskar",
            "supplier_parts": "KIRL-500-A, PUMP-IND-500",
            "specifications_raw": "Power: 5HP\nVoltage: 415V\nPhase: 3-Phase"
        },
        follow_redirects=True
    )
    assert res.status_code == 200
    assert sku in res.text
    assert "Centrifugal Industrial Water Pump" in res.text

    # 3. View detail
    created_item = services.get_item_master_item(sku)
    assert created_item is not None
    assert created_item.stocking_uom == "NOS"
    assert created_item.brand == "Kirloskar"

    res_detail = client.get(f"/item-master/{created_item.internal_item_id}")
    assert res_detail.status_code == 200
    assert sku in res_detail.text
    assert "KIRL-500-A" in res_detail.text
    assert "415V" in res_detail.text


def test_import_catalog_excel_with_column_variations(client: TestClient):
    """Verifies importing catalog using CatalogExtractor with column name variations (Product Code, Item Description)."""
    # 1. Build test workbook with enterprise column variations
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Master Catalog"
    ws.append(["Company Master Catalog 2026"])
    ws.append(["Product Code", "Item Description", "Mfr Part No", "Stock Unit", "Brand Name", "Rating"])
    ws.append(["VALV-BALL-3IN", "Industrial Flanged Ball Valve 3-inch Class 150", "BV-150-3IN", "PCS", "L&T", "Class 150"])
    ws.append(["GAUGE-PRESS-10B", "Digital Pressure Gauge 0-10 Bar SS316", "PG-10B-DIG", "NOS", "WIKA", "0-10 Bar"])
    buf = io.BytesIO()
    wb.save(buf)
    excel_bytes = buf.getvalue()

    # 2. Upload & Preview
    res_prev = client.post(
        "/item-master/import/preview",
        files={"file": ("company_catalog.xlsx", excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert res_prev.status_code == 200
    assert "Preview Extracted Catalog Products" in res_prev.text
    assert "Detected Catalog Structure" in res_prev.text
    assert "VALV-BALL-3IN" in res_prev.text
    assert "GAUGE-PRESS-10B" in res_prev.text
    assert "L&T" in res_prev.text

    # 3. Confirm Import
    import_items = [
        {
            "internal_sku": "VALV-BALL-3IN",
            "canonical_description": "Industrial Flanged Ball Valve 3-inch Class 150",
            "manufacturer_part_number": "BV-150-3IN",
            "stocking_uom": "PCS",
            "brand": "L&T"
        },
        {
            "internal_sku": "GAUGE-PRESS-10B",
            "canonical_description": "Digital Pressure Gauge 0-10 Bar SS316",
            "manufacturer_part_number": "PG-10B-DIG",
            "stocking_uom": "NOS",
            "brand": "WIKA"
        }
    ]
    res_conf = client.post(
        "/item-master/import/confirm",
        data={"items_json": json.dumps(import_items), "mode": "append"},
        follow_redirects=True
    )
    assert res_conf.status_code == 200
    assert "VALV-BALL-3IN" in res_conf.text
    assert "GAUGE-PRESS-10B" in res_conf.text


def test_import_catalog_csv_with_warnings(client: TestClient):
    """Verifies importing CSV with missing UOMs generating validation warnings."""
    csv_content = """Item Code,Product Name,Brand,Category
TRANS-500KVA,Distribution Transformer 500kVA 11kV/415V,ABB,Electrical
TRANS-500KVA,Distribution Transformer 500kVA 11kV/415V Duplicate,ABB,Electrical
CBL-LUG-50SQ,Copper Compression Cable Lug 50 Sq.mm,Dowells,Fasteners
"""
    res_prev = client.post(
        "/item-master/import/preview",
        files={"file": ("transformers.csv", csv_content.encode("utf-8"), "text/csv")}
    )
    assert res_prev.status_code == 200
    assert "QUALITY AUDIT" in res_prev.text
    assert "Duplicate SKU" in res_prev.text
    assert "TRANS-500KVA" in res_prev.text
    assert "CBL-LUG-50SQ" in res_prev.text


def test_non_catalog_document_rejection_diagnostic(client: TestClient):
    """Verifies that non-catalog files produce a clear diagnostic screen rather than silent failure."""
    non_catalog_csv = """Notice of Meeting
The annual procurement committee will convene on Monday.
Please review the attached agenda.
"""
    res_prev = client.post(
        "/item-master/import/preview",
        files={"file": ("meeting_notes.csv", non_catalog_csv.encode("utf-8"), "text/csv")}
    )
    assert res_prev.status_code == 200
    assert "CATALOG EXTRACTION BLOCKED" in res_prev.text
    assert "Document Structure Not Recognized as a Product Catalog" in res_prev.text


def test_rfq_creation_uses_company_item_master(client: TestClient):
    """Verifies that RFQ creation allows picking products from the Item Master."""
    res = client.get("/rfqs/create")
    assert res.status_code == 200
    assert ("Select Products from Company Item Master" in res.text or "Select from Company Catalog" in res.text or "Company Catalog" in res.text)
    assert "BEAR-6205-2RS" in res.text

    rfq_items = [
        {"rfq_line_id": "RFQ-LINE-001", "sku": "BEAR-6205-2RS", "description": "Deep Groove Ball Bearing 25x52x15mm", "requested_quantity": 250, "requested_uom": "PCS"},
        {"rfq_line_id": "RFQ-LINE-002", "sku": "VALV-BALL-3IN", "description": "Industrial Flanged Ball Valve 3-inch", "requested_quantity": 10, "requested_uom": "PCS"}
    ]

    res_post = client.post(
        "/rfqs",
        data={
            "rfq_id": "RFQ-TEST-CATALOG-01",
            "title": "Catalog Based Procurement RFQ",
            "base_currency": "INR",
            "items_json": json.dumps(rfq_items)
        },
        follow_redirects=True
    )
    assert res_post.status_code == 200
    assert "Catalog Based Procurement RFQ" in res_post.text
    assert "VALV-BALL-3IN" in res_post.text
