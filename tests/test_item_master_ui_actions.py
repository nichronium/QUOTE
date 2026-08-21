import uuid
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import services
from matching.models import ItemMasterRecord


@pytest.fixture
def client():
    return TestClient(app)


def test_ui_action_wiring_and_modals_present(client: TestClient):
    """Verifies that Item Master HTML contains distinct Deactivate and Delete modals with correct wording."""
    res = client.get("/item-master")
    assert res.status_code == 200
    html = res.text

    # 1. Deactivate modal must exist and NOT contain 'permanently delete'
    assert 'id="deactivateItemModal"' in html
    assert "Deactivate Item?" in html
    assert "This item will become" in html
    assert "INACTIVE" in html
    assert "Historical RFQs, comparisons, and awards remain unchanged." in html

    # 2. Permanent Delete modal must exist and contain safe delete protection warning
    assert 'id="deleteItemModal"' in html
    assert "Permanently Delete Item?" in html
    assert "This permanently removes the Item Master record" in html
    assert "If historical references exist, deletion will be rejected" in html

    # 3. Row action buttons must wire to distinct modal openers
    assert "openDeactivateModal" in html
    assert "openDeleteModal" in html
    assert "restoreItem" in html


def test_scenario_a_deactivate_flow(client: TestClient):
    """TEST A — Deactivate an unused ACTIVE item."""
    uid = uuid.uuid4().hex[:6]
    sku = f"DEACT-FLOW-{uid}"
    item = services.add_item_to_master(sku, "Deactivate Flow Item", "PCS")
    item_id = item.internal_item_id

    # 1. Verify item is in catalog and ACTIVE
    rec = services.get_item_master_item(item_id)
    assert rec is not None
    assert rec.status == "ACTIVE"

    # 2. Trigger Deactivate endpoint (as clicked in UI modal)
    res = client.post(f"/api/item-master/{item_id}/deactivate")
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # 3. Verify item remains in Item Master with status INACTIVE
    updated = services.get_item_master_item(item_id)
    assert updated is not None
    assert updated.status == "INACTIVE"

    # 4. Verify no JSON record is deleted (it's in all items)
    all_items = services.get_item_master(include_inactive=True)
    assert any(it.internal_item_id == item_id for it in all_items)

    # 5. Verify item is excluded from new active catalog selection
    active_items = services.get_item_master(include_inactive=False)
    assert all(it.internal_item_id != item_id for it in active_items)


def test_scenario_b_restore_flow(client: TestClient):
    """TEST B — Restore the INACTIVE item back to ACTIVE."""
    uid = uuid.uuid4().hex[:6]
    sku = f"REST-FLOW-{uid}"
    item = services.add_item_to_master(sku, "Restore Flow Item", "PCS")
    item_id = item.internal_item_id

    # Deactivate first
    services.deactivate_item_master_record(item_id)
    assert services.get_item_master_item(item_id).status == "INACTIVE"

    # Trigger Restore endpoint (as clicked in UI)
    res = client.post(f"/api/item-master/{item_id}/restore")
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Verify status returns to ACTIVE
    updated = services.get_item_master_item(item_id)
    assert updated is not None
    assert updated.status == "ACTIVE"

    # Verify present in active catalog
    active_items = services.get_item_master(include_inactive=False)
    assert any(it.internal_item_id == item_id for it in active_items)


def test_scenario_c_permanent_delete_unused_item(client: TestClient):
    """TEST C — Permanent Delete of an unreferenced item."""
    uid = uuid.uuid4().hex[:6]
    sku = f"DEL-FLOW-{uid}"
    item = services.add_item_to_master(sku, "Delete Flow Item", "PCS")
    item_id = item.internal_item_id

    assert services.get_item_master_item(item_id) is not None

    # Trigger Delete endpoint (as clicked in UI modal)
    res = client.post(f"/api/item-master/{item_id}/delete")
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Verify actually removed from catalog
    assert services.get_item_master_item(item_id) is None
    all_items = services.get_item_master(include_inactive=True)
    assert all(it.internal_item_id != item_id for it in all_items)


def test_scenario_d_referenced_delete_is_blocked(client: TestClient):
    """TEST D — Attempt Delete on a referenced item -> MUST BE REJECTED."""
    uid = uuid.uuid4().hex[:6]
    sku = f"REF-DEL-FLOW-{uid}"
    item = services.add_item_to_master(sku, "Referenced Delete Flow Item", "PCS")
    item_id = item.internal_item_id

    # Reference in an RFQ
    rfq_id = f"RFQ-REF-DEL-{uid}"
    services.create_rfq(rfq_id, "Referenced Delete Test RFQ", "INR", [
        {"sku": item.internal_sku, "description": item.canonical_description, "internal_item_id": item.internal_item_id, "requested_quantity": "50", "requested_uom": "PCS"}
    ])

    # Trigger Delete endpoint -> Must fail with 400
    res = client.post(f"/api/item-master/{item_id}/delete")
    assert res.status_code == 400
    err_data = res.json()
    assert err_data["status"] == "error"
    assert "Cannot hard delete" in err_data["message"]
    assert "Deactivate the item instead" in err_data["message"]

    # Verify item is NOT removed
    assert services.get_item_master_item(item_id) is not None


def test_scenario_e_historical_integrity_after_deactivation(client: TestClient):
    """TEST E — Deactivate an item referenced by an existing RFQ/comparison/award and verify pages still resolve."""
    uid = uuid.uuid4().hex[:6]
    sku = f"HIST-DEACT-{uid}"
    item = services.add_item_to_master(sku, "Historical Item With Special Specs", "PCS", specifications={"Material": "SS316"})
    item_id = item.internal_item_id

    rfq_id = f"RFQ-HIST-DEACT-{uid}"
    services.create_rfq(rfq_id, "Historical Deact RFQ", "INR", [
        {"sku": item.internal_sku, "description": item.canonical_description, "internal_item_id": item.internal_item_id, "requested_quantity": "100", "requested_uom": "PCS"}
    ])

    # Deactivate item
    services.deactivate_item_master_record(item_id)
    assert services.get_item_master_item(item_id).status == "INACTIVE"

    # Verify RFQ detail page still resolves and renders the item correctly
    res = client.get(f"/rfqs/{rfq_id}")
    assert res.status_code == 200
    assert sku.upper() in res.text
    assert "Historical Item With Special Specs" in res.text

    # Verify Item Master detail page still resolves
    res_detail = client.get(f"/item-master/{item_id}")
    assert res_detail.status_code == 200
    assert sku.upper() in res_detail.text


def test_item_counters_invariant(client: TestClient):
    """Verifies that active + inactive == total count in Item Master."""
    items = services.get_item_master(include_inactive=True)
    total_count = len(items)
    active_count = sum(1 for it in items if it.status != "INACTIVE")
    inactive_count = sum(1 for it in items if it.status == "INACTIVE")

    assert active_count + inactive_count == total_count

    res = client.get("/item-master")
    assert res.status_code == 200
    assert str(total_count) in res.text
    assert str(active_count) in res.text
    assert str(inactive_count) in res.text
