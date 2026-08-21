import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import services


@pytest.fixture
def client():
    return TestClient(app)


def test_settings_page_and_themes_rendering(client: TestClient):
    res = client.get("/settings")
    assert res.status_code == 200
    # Verify theme controls are present
    assert "theme-btn-velvet-slate" in res.text
    assert "theme-btn-champagne-silk" in res.text
    assert "theme-btn-aurora-night" in res.text
    assert "theme-btn-misty-amethyst" in res.text
    assert "theme-btn-rose-noir" in res.text
    assert "theme-btn-alpine-frost" in res.text

    # Verify accent controls are present
    assert "accent-btn-blue" in res.text
    assert "accent-btn-emerald" in res.text
    assert "accent-btn-purple" in res.text
    assert "accent-btn-amber" in res.text
    assert "accent-btn-rose" in res.text
    assert "accent-btn-cyan" in res.text


def test_no_topbar_search_on_any_page(client: TestClient):
    # Verify searchbar is removed from all pages
    pages = ["/", "/rfqs", "/item-master", "/review", "/comparisons", "/settings", "/analytics"]
    for p in pages:
        res = client.get(p)
        assert res.status_code == 200
        assert "globalSearchInput" not in res.text
        assert "search-container" not in res.text


def test_save_settings_api(client: TestClient):
    payload = {
        "company_name": "Test Global Procurement Corp",
        "default_currency": "EUR",
        "default_allocation": "EQUAL_SPLIT_ACROSS_LINES",
        "variance_tolerance_pct": "7.5"
    }
    res = client.post("/api/settings", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["settings"]["company_name"] == "Test Global Procurement Corp"
    assert data["settings"]["default_currency"] == "EUR"

    # Verify get settings returns updated values
    s = services.get_application_settings()
    assert s["company_name"] == "Test Global Procurement Corp"
    assert s["default_currency"] == "EUR"


def test_seed_demo_and_export_data(client: TestClient):
    res_seed = client.post("/api/settings/seed-demo")
    assert res_seed.status_code == 200
    assert res_seed.json()["status"] == "success"

    res_export = client.get("/api/settings/export")
    assert res_export.status_code == 200
    exp_data = res_export.json()
    assert "item_master" in exp_data
    assert "rfqs" in exp_data


def test_reset_procurement_data_api(client: TestClient):
    res_reset = client.post("/api/settings/reset")
    assert res_reset.status_code == 200
    assert res_reset.json()["status"] == "success"

    # Verify user RFQs are empty after reset
    rfqs = services.list_rfqs(include_demo=False)
    assert len(rfqs) == 0

    # Verify item master catalog is preserved
    im = services.get_item_master()
    assert len(im) > 0
