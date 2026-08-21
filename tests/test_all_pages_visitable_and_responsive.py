import json
import time
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import services


@pytest.fixture
def client():
    return TestClient(app)


def test_every_application_page_can_be_visited(client: TestClient):
    """
    Verifies that every single page and route in Quote Intelligence can be visited cleanly (200 OK),
    verifies that Home does not render the top searchbar, and verifies that Settings and Analytics work.
    """
    # 1. Home Page - ensure searchbar is removed from the topbar
    res_home = client.get("/")
    assert res_home.status_code == 200
    assert "Quote Intelligence" in res_home.text
    assert "id=\"globalSearchInput\"" not in res_home.text  # Removed from Home topbar

    # 2. Settings Page
    res_settings = client.get("/settings")
    assert res_settings.status_code == 200
    assert "Application &amp; Company Preferences" in res_settings.text or "Application & Company Preferences" in res_settings.text
    assert "id=\"globalSearchInput\"" not in res_settings.text  # Searchbar present on inner pages

    # 3. Analytics Page
    res_analytics = client.get("/analytics")
    assert res_analytics.status_code == 200
    assert "Procurement Analytics &amp; Sourcing Intelligence" in res_analytics.text or "Procurement Analytics & Sourcing Intelligence" in res_analytics.text

    # 4. RFQ Hub & Creation Pages
    res_rfqs = client.get("/rfqs")
    assert res_rfqs.status_code == 200

    res_rfqs_create = client.get("/rfqs/create")
    assert res_rfqs_create.status_code == 200
    assert "Create Procurement RFQ" in res_rfqs_create.text

    # 5. Item Master Hub, Import & Create Pages
    res_im = client.get("/item-master")
    assert res_im.status_code == 200

    res_im_import = client.get("/item-master/import")
    assert res_im_import.status_code == 200
    assert "Import Product Catalog" in res_im_import.text

    res_im_new = client.get("/item-master/new")
    assert res_im_new.status_code == 200
    assert "Add Product to Catalog" in res_im_new.text

    # 6. Quotes Hub & Upload Pages
    res_quotes = client.get("/quotes")
    assert res_quotes.status_code == 200

    res_quotes_upload = client.get("/quotes/upload")
    assert res_quotes_upload.status_code == 200

    # 7. Comparisons Hub & Create Pages
    res_comparisons = client.get("/comparisons")
    assert res_comparisons.status_code == 200

    res_comparisons_create = client.get("/comparisons/create")
    assert res_comparisons_create.status_code == 200

    # 8. Review Center
    res_review = client.get("/review")
    assert res_review.status_code == 200
    assert "Actionable Review Center" in res_review.text

    # 9. Dynamic RFQ Workflow Pages for a created RFQ
    ts = int(time.time() * 1000)
    test_rfq_id = f"RFQ-PAGE-TEST-{ts}"
    client.post(
        "/rfqs",
        data={
            "rfq_id": test_rfq_id,
            "title": "Page Visitability Test RFQ",
            "base_currency": "INR",
            "items_json": json.dumps([{"rfq_line_id": "LINE-1", "sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": 50, "requested_uom": "PCS"}])
        },
        follow_redirects=True
    )

    # RFQ Workspace
    res_rfq_ws = client.get(f"/rfqs/{test_rfq_id}")
    assert res_rfq_ws.status_code == 200
    assert "Page Visitability Test RFQ" in res_rfq_ws.text

    # Comparison shortcut & setup for this RFQ
    res_rfq_comp_setup = client.get(f"/rfqs/{test_rfq_id}/comparison/setup")
    assert res_rfq_comp_setup.status_code == 200

    res_rfq_compare = client.get(f"/rfqs/{test_rfq_id}/compare", follow_redirects=True)
    assert res_rfq_compare.status_code == 200

    # Award shortcut for this RFQ
    res_rfq_award = client.get(f"/rfqs/{test_rfq_id}/award", follow_redirects=True)
    assert res_rfq_award.status_code == 200
