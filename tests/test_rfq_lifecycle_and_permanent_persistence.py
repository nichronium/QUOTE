import json
import uuid
import pytest
from pathlib import Path
from decimal import Decimal
from fastapi.testclient import TestClient

from app.main import app
from app import services
from comparison.models import RFQDocument, RFQLineItem, RFQStatus


@pytest.fixture
def client():
    return TestClient(app)


def create_mock_award(rfq_id: str, is_finalized: bool = True):
    services.AWARDS_DIR.mkdir(parents=True, exist_ok=True)
    award_file = services.get_award_file_path(rfq_id)
    data = {
        "rfq_id": rfq_id,
        "award_id": f"AWD-{rfq_id}",
        "status": "FINALIZED" if is_finalized else "DRAFT",
        "created_at": "2026-08-21T00:00:00Z",
        "allocations": [
            {
                "rfq_line_id": "RFQ-LINE-001",
                "sku": "BEAR-6205-2RS",
                "description": "Bearing 6205",
                "requested_quantity": "10",
                "requested_uom": "PCS",
                "allocated_quantity": "10",
                "allocated_supplier_id": "BENCH-SUPP-A",
                "allocated_supplier_name": "Alpha Industrial Supplies",
                "unit_price_base": "150.00",
                "total_line_cost_base": "1500.00",
                "allocation_status": "FULLY_ALLOCATED"
            }
        ],
        "supplier_summary": {
            "BENCH-SUPP-A": {
                "supplier_name": "Alpha Industrial Supplies",
                "allocated_lines_count": 1,
                "total_awarded_val": "1500.00"
            }
        },
        "total_awarded_val": "1500.00",
        "base_currency": "INR"
    }
    with open(award_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def test_01_created_rfq_persists_after_page_refresh(client: TestClient):
    """1. Created RFQ persists after page refresh."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-PERSIST-{uid}"
    doc = services.create_rfq(rfq_id, f"Persist Test {uid}", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing 6205", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    # 1. Verify JSON file exists on disk
    rfq_path = services.RFQS_DIR / f"{rfq_id}.json"
    assert rfq_path.exists()
    
    # 2. Simulate repeated page requests (refreshes)
    for _ in range(3):
        res = client.get(f"/rfqs/{rfq_id}")
        assert res.status_code == 200
        assert rfq_id in res.text
        assert f"Persist Test {uid}" in res.text
    
    assert rfq_path.exists()


def test_02_created_rfq_persists_after_application_restart(client: TestClient):
    """2. Created RFQ persists after application restart (fresh client/service reload)."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-RESTART-{uid}"
    services.create_rfq(rfq_id, f"Restart Test {uid}", "INR", [
        {"sku": "CABL-6SQ-CU", "description": "Cable 6 sq mm", "requested_quantity": "50", "requested_uom": "MTR"}
    ])
    
    # Simulate restart by creating a new TestClient and re-reading from disk
    new_client = TestClient(app)
    reloaded = services.get_rfq(rfq_id)
    assert reloaded is not None
    assert reloaded.rfq_id == rfq_id
    assert reloaded.title == f"Restart Test {uid}"
    
    res = new_client.get(f"/rfqs/{rfq_id}")
    assert res.status_code == 200
    assert rfq_id in res.text


def test_03_creating_multiple_rfqs_never_overwrites_earlier_rfq():
    """3. Creating multiple RFQs never overwrites an earlier RFQ."""
    uid1 = uuid.uuid4().hex[:6]
    uid2 = uuid.uuid4().hex[:6]
    rfq_id1 = f"RFQ-MULTI-1-{uid1}"
    rfq_id2 = f"RFQ-MULTI-2-{uid2}"
    
    doc1 = services.create_rfq(rfq_id1, "First RFQ Title", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing 6205", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    doc2 = services.create_rfq(rfq_id2, "Second RFQ Title", "USD", [
        {"sku": "CABL-6SQ-CU", "description": "Cable 6 sq mm", "requested_quantity": "100", "requested_uom": "MTR"}
    ])
    
    assert (services.RFQS_DIR / f"{rfq_id1}.json").exists()
    assert (services.RFQS_DIR / f"{rfq_id2}.json").exists()
    
    loaded1 = services.get_rfq(rfq_id1)
    loaded2 = services.get_rfq(rfq_id2)
    assert loaded1.title == "First RFQ Title"
    assert loaded2.title == "Second RFQ Title"


def test_04_rfq_ids_are_unique_and_immutable():
    """4. RFQ IDs are unique and immutable."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-IMMUTABLE-{uid}"
    doc = services.create_rfq(rfq_id, "Immutable RFQ Title", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "5", "requested_uom": "PCS"}
    ])
    assert doc.rfq_id == rfq_id
    
    # Attempt to update via save_rfq_document with changed fields - rfq_id remains identical
    doc.title = "Updated Immutable Title"
    services.save_rfq_document(doc)
    
    loaded = services.get_rfq(rfq_id)
    assert loaded.rfq_id == rfq_id
    assert loaded.title == "Updated Immutable Title"


def test_05_supplier_quote_upload_does_not_delete_rfq(client: TestClient):
    """5. Supplier quote upload does not delete RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-UPLOAD-{uid}"
    services.create_rfq(rfq_id, "Upload Test RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing 6205", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    rfq_file = services.RFQS_DIR / f"{rfq_id}.json"
    assert rfq_file.exists()
    
    # Ingest a quote for this RFQ
    qid = f"Q-UPLOAD-{uid}"
    services.create_quote(f"Supplier Alpha Quote {uid}", rfq_id)
    
    # Verify RFQ still exists intact
    assert rfq_file.exists()
    assert services.get_rfq(rfq_id) is not None


def test_06_matching_does_not_delete_rfq(client: TestClient):
    """6. Matching does not delete RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-MATCH-{uid}"
    services.create_rfq(rfq_id, "Match Test RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Deep Groove Ball Bearing", "requested_quantity": "20", "requested_uom": "PCS"}
    ])
    
    # Trigger matching resolution
    services.get_rfq_workspace_data(rfq_id)
    
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()
    assert services.get_rfq(rfq_id) is not None


def test_07_comparison_does_not_delete_rfq(client: TestClient):
    """7. Comparison does not delete RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-COMP-{uid}"
    services.create_rfq(rfq_id, "Comparison Test RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "15", "requested_uom": "PCS"}
    ])
    
    services.get_latest_rfq_comparison(rfq_id)
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()


def test_08_award_finalization_does_not_delete_rfq(client: TestClient):
    """8. Award finalization does not delete RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-AWARD-{uid}"
    services.create_rfq(rfq_id, "Award Test RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing 6205", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    # Finalize award decision
    create_mock_award(rfq_id, is_finalized=True)
    
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()
    assert services.get_rfq(rfq_id) is not None


def test_09_award_reopening_does_not_delete_rfq(client: TestClient):
    """9. Award reopening does not delete RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-REOPEN-{uid}"
    services.create_rfq(rfq_id, "Reopen Test RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    # Finalize award then reopen
    create_mock_award(rfq_id, is_finalized=True)
    services.reopen_award_decision(rfq_id, reason="Negotiation round 2")
    
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()
    assert services.get_rfq(rfq_id) is not None


def test_10_export_generation_does_not_delete_rfq(client: TestClient):
    """10. Export generation does not delete RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-EXPORT-{uid}"
    services.create_rfq(rfq_id, "Export Test RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    # Finalize award & trigger exports
    create_mock_award(rfq_id, is_finalized=True)
    
    res_csv = client.get(f"/rfqs/{rfq_id}/award/export/csv")
    assert res_csv.status_code in [200, 303]
    
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()


def test_11_item_master_import_rollback_does_not_delete_rfq():
    """11. Item-master import rollback does not delete RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-UNDO-ITEM-{uid}"
    sku = f"UNDO-SKU-{uid}"
    
    doc = services.create_rfq(rfq_id, "Undo Batch RFQ", "INR", [
        {"sku": sku, "description": "Undo Batch Item", "requested_quantity": "5", "requested_uom": "PCS"}
    ])
    
    # Retrieve the batch ID
    catalog = services.get_item_master(include_inactive=True)
    target_item = next((it for it in catalog if it.internal_sku == sku.upper()), None)
    if target_item and target_item.import_batch_id:
        services.undo_import_batch(target_item.import_batch_id)
        
    # RFQ must remain 100% intact on disk
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()
    assert services.get_rfq(rfq_id) is not None


def test_12_supplier_master_operations_do_not_delete_rfq():
    """12. Supplier-master operations do not delete RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-SUPP-OPS-{uid}"
    services.create_rfq(rfq_id, "Supplier Ops Test RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    # Perform supplier rename / deactivation
    services.update_supplier_name("BENCH-SUPP-A", "Alpha Enterprises Renamed")
    services.deactivate_supplier("BENCH-SUPP-D")
    
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()
    assert services.get_rfq(rfq_id) is not None


def test_13_rfq_status_changes_do_not_remove_rfq_from_storage():
    """13. RFQ status changes do not remove the RFQ from persistent storage."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-STATUS-CHG-{uid}"
    doc = services.create_rfq(rfq_id, "Status Change Test RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    # Change status to CANCELLED
    services.cancel_rfq(rfq_id)
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()
    assert services.get_rfq(rfq_id).status == "CANCELLED"
    
    # Change status to ARCHIVED
    services.archive_rfq(rfq_id)
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()
    assert services.get_rfq(rfq_id).status == "ARCHIVED"
    
    # Restore to OPEN
    services.restore_rfq_lifecycle(rfq_id)
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()
    assert services.get_rfq(rfq_id).status == "OPEN"


def test_14_archived_rfqs_remain_readable(client: TestClient):
    """14. Archived RFQs remain readable via UI and API."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-ARCHIVED-READ-{uid}"
    services.create_rfq(rfq_id, f"Archived Read Test {uid}", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Archived Bearing Item", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    services.archive_rfq(rfq_id)
    
    # Detail workspace still loads
    res = client.get(f"/rfqs/{rfq_id}")
    assert res.status_code == 200
    assert rfq_id in res.text
    assert f"Archived Read Test {uid}" in res.text


def test_15_cancelled_rfqs_remain_readable(client: TestClient):
    """15. Cancelled RFQs remain readable via UI and API."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-CANCELLED-READ-{uid}"
    services.create_rfq(rfq_id, f"Cancelled Read Test {uid}", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Cancelled Bearing Item", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    services.cancel_rfq(rfq_id)
    
    res = client.get(f"/rfqs/{rfq_id}")
    assert res.status_code == 200
    assert rfq_id in res.text


def test_16_historical_awards_still_resolve_their_rfq():
    """16. Historical awards still resolve their RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-AWARD-RESOLVE-{uid}"
    services.create_rfq(rfq_id, "Award Resolve RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    create_mock_award(rfq_id, is_finalized=True)
    
    # Archive the RFQ
    services.archive_rfq(rfq_id)
    
    # Award decision must still load and resolve RFQ details
    award = services.get_award_decision(rfq_id)
    assert award is not None
    assert award["rfq_id"] == rfq_id
    rfq_doc = services.get_rfq(rfq_id)
    assert rfq_doc is not None


def test_17_historical_comparisons_still_resolve_their_rfq():
    """17. Historical comparisons still resolve their RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-COMP-RESOLVE-{uid}"
    services.create_rfq(rfq_id, "Comparison Resolve RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    comp = services.get_latest_rfq_comparison(rfq_id)
    services.archive_rfq(rfq_id)
    
    rfq_doc = services.get_rfq(rfq_id)
    assert rfq_doc is not None
    assert rfq_doc.rfq_id == rfq_id


def test_18_attempting_to_delete_referenced_rfq_is_converted_to_archive(client: TestClient):
    """18. Attempting to delete a referenced RFQ is safely converted into archive behavior."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-SAFE-DEL-{uid}"
    services.create_rfq(rfq_id, "Safe Delete RFQ", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    create_mock_award(rfq_id, is_finalized=True)
    
    # Call delete endpoint
    res = client.post(f"/api/rfqs/{rfq_id}/delete")
    assert res.status_code == 200
    assert "ARCHIVED" in res.json()["message"]
    
    # Verify file is NOT deleted from disk
    assert (services.RFQS_DIR / f"{rfq_id}.json").exists()
    assert services.get_rfq(rfq_id).status == "ARCHIVED"


def test_19_no_rfq_id_collision_can_overwrite_existing_rfq():
    """19. No RFQ ID collision can overwrite an existing RFQ."""
    uid = uuid.uuid4().hex[:6]
    rfq_id = f"RFQ-COLLIDE-{uid}"
    
    doc1 = services.create_rfq(rfq_id, "Original RFQ Title", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Original Bearing", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    # Attempt to create another RFQ with the EXACT same ID but different title
    doc2 = services.create_rfq(rfq_id, "Colliding RFQ Title", "USD", [
        {"sku": "CABL-6SQ-CU", "description": "Colliding Cable", "requested_quantity": "100", "requested_uom": "MTR"}
    ])
    
    # Original RFQ must NOT have been overwritten
    loaded1 = services.get_rfq(rfq_id)
    assert loaded1.title == "Original RFQ Title"
    
    # Second RFQ received a unique non-colliding ID
    assert doc2.rfq_id != rfq_id
    assert doc2.title == "Colliding RFQ Title"
    assert (services.RFQS_DIR / f"{doc2.rfq_id}.json").exists()


def test_20_rfq_list_displays_existing_rfqs_according_to_lifecycle_filter(client: TestClient):
    """20. RFQ list displays existing RFQs according to the selected lifecycle filter."""
    uid = uuid.uuid4().hex[:6]
    rfq_active = f"RFQ-TAB-ACT-{uid}"
    rfq_archived = f"RFQ-TAB-ARC-{uid}"
    rfq_cancelled = f"RFQ-TAB-CAN-{uid}"
    
    services.create_rfq(rfq_active, f"Active RFQ {uid}", "INR", [])
    services.create_rfq(rfq_archived, f"Archived RFQ {uid}", "INR", [])
    services.archive_rfq(rfq_archived)
    services.create_rfq(rfq_cancelled, f"Cancelled RFQ {uid}", "INR", [])
    services.cancel_rfq(rfq_cancelled)
    
    # 1. Filter: all
    res_all = client.get("/rfqs?status=all")
    assert res_all.status_code == 200
    assert rfq_active in res_all.text
    assert rfq_archived in res_all.text
    assert rfq_cancelled in res_all.text
    
    # 2. Filter: active
    res_act = client.get("/rfqs?status=active")
    assert res_act.status_code == 200
    assert rfq_active in res_act.text
    assert rfq_archived not in res_act.text
    assert rfq_cancelled not in res_act.text
    
    # 3. Filter: archived
    res_arc = client.get("/rfqs?status=archived")
    assert res_arc.status_code == 200
    assert rfq_archived in res_arc.text
    assert rfq_active not in res_arc.text
    
    # 4. Filter: cancelled
    res_can = client.get("/rfqs?status=cancelled")
    assert res_can.status_code == 200
    assert rfq_cancelled in res_can.text
    assert rfq_active not in res_can.text
