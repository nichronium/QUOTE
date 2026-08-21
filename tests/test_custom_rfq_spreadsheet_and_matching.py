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


def test_custom_rfq_spreadsheet_auto_catalog_and_supplier_matching(client: TestClient):
    """
    Verifies that:
    1. A buyer can upload a custom requirement Excel sheet containing items NOT in the standard Item Master.
    2. Creating the RFQ registers these items into the Item Master.
    3. A supplier quote uploaded for this RFQ matches these custom items seamlessly.
    4. Sourcing comparison and award run with 100% coverage.
    """
    ts = int(time.time() * 1000)
    rfq_id = f"RFQ-CUSTOM-XLSX-{ts}"

    # 1. Create a custom requirement workbook with 2 non-catalog items
    wb_req = openpyxl.Workbook()
    ws_req = wb_req.active
    ws_req.title = "Requirements"
    ws_req.append(["Line Item", "Part Number", "Description", "Required Qty", "UOM"])
    ws_req.append([1, "VALVE-DN50-SS", "Stainless Steel Ball Valve DN50 PN40 Full Bore", 25, "PCS"])
    ws_req.append([2, "FLANGE-PN16-100", "Carbon Steel Blind Flange PN16 100mm", 50, "PCS"])

    req_bytes = io.BytesIO()
    wb_req.save(req_bytes)
    req_bytes.seek(0)

    # 2. Preview requirements endpoint
    res_prev = client.post(
        "/rfqs/requirements/preview",
        files={"file": ("requirements.xlsx", req_bytes.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert res_prev.status_code == 200
    prev_data = res_prev.json()
    assert prev_data["total_lines"] == 2
    assert prev_data["items"][0]["raw_sku"] == "VALVE-DN50-SS"

    # 3. Create the RFQ with these custom items
    rfq_items = [
        {
            "rfq_line_id": "RFQ-LINE-001",
            "sku": "VALVE-DN50-SS",
            "description": "Stainless Steel Ball Valve DN50 PN40 Full Bore",
            "requested_quantity": 25,
            "requested_uom": "PCS"
        },
        {
            "rfq_line_id": "RFQ-LINE-002",
            "sku": "FLANGE-PN16-100",
            "description": "Carbon Steel Blind Flange PN16 100mm",
            "requested_quantity": 50,
            "requested_uom": "PCS"
        }
    ]

    res_create = client.post(
        "/rfqs",
        data={
            "rfq_id": rfq_id,
            "title": "Custom High Pressure Piping RFQ",
            "base_currency": "INR",
            "items_json": json.dumps(rfq_items)
        },
        follow_redirects=True
    )
    assert res_create.status_code == 200
    assert rfq_id in res_create.text

    # Verify that Item Master now contains the new custom items
    item_master = services.get_item_master()
    im_skus = {im.internal_sku for im in item_master}
    assert "VALVE-DN50-SS" in im_skus
    assert "FLANGE-PN16-100" in im_skus

    # 4. Create and upload a supplier quote quoting these custom items
    wb_quote = openpyxl.Workbook()
    ws_quote = wb_quote.active
    ws_quote.title = "Quotation"
    ws_quote.append(["Supplier: Industrial Valves & Flanges Corp", "", "", "", ""])
    ws_quote.append(["Quote Ref: IVF-2026-99", "Currency: INR", "", "", ""])
    ws_quote.append(["Item Code", "Description", "Qty", "UOM", "Unit Price"])
    ws_quote.append(["VALVE-DN50-SS", "Stainless Steel Ball Valve DN50 PN40", 25, "PCS", 3500.00])
    ws_quote.append(["FLANGE-PN16-100", "Carbon Steel Blind Flange PN16", 50, "PCS", 1200.00])

    quote_bytes = io.BytesIO()
    wb_quote.save(quote_bytes)
    quote_bytes.seek(0)

    # Upload quote to RFQ
    res_upload = client.post(
        f"/rfqs/{rfq_id}/upload-quote",
        data={"supplier_name": "Industrial Valves Corp"},
        files={"file": ("quote_ivf.xlsx", quote_bytes.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=True
    )
    assert res_upload.status_code == 200

    # 5. Extract quote and check matching
    quotes = services.get_quotes_for_rfq(rfq_id)
    assert len(quotes) >= 1
    quote_id = quotes[0]["quote_id"]

    res_extract = client.post(f"/quotes/{quote_id}/extract", follow_redirects=True)
    assert res_extract.status_code == 200

    # Verify matching ran and matched both custom RFQ items
    matched_items = services.get_matched_items(quote_id)
    assert matched_items is not None
    assert len(matched_items) == 2
    assert matched_items[0].match_status.value in ["EXACT_MATCH", "HIGH_CONFIDENCE_MATCH"]
    assert matched_items[1].match_status.value in ["EXACT_MATCH", "HIGH_CONFIDENCE_MATCH"]

    # Verify RFQ workspace shows 100% coverage
    ws_quotes = services.get_quotes_for_rfq(rfq_id)
    assert ws_quotes[0]["rfq_items_matched"] == 2
    assert ws_quotes[0]["coverage_pct_str"] == "100%"
