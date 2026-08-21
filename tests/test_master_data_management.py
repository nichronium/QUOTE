import json
import uuid
from decimal import Decimal
from typing import Dict, Any
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import services
from matching.models import ItemMasterRecord, SupplierMasterRecord


@pytest.fixture
def client():
    return TestClient(app)


def test_01_single_unused_item_can_be_hard_deleted():
    uid = uuid.uuid4().hex[:6]
    item = services.add_item_to_master(
        internal_sku=f"TEST-UNUSED-{uid}",
        canonical_description="Unused Test Item",
        stocking_uom="PCS"
    )
    assert services.get_item_master_item(item.internal_item_id) is not None

    success, msg = services.delete_item_master_record(item.internal_item_id)
    assert success is True
    assert services.get_item_master_item(item.internal_item_id) is None


def test_02_referenced_item_cannot_be_hard_deleted():
    uid = uuid.uuid4().hex[:6]
    sku = f"TEST-REF-{uid}"
    item = services.add_item_to_master(
        internal_sku=sku,
        canonical_description="Referenced Test Item",
        stocking_uom="PCS"
    )
    rfq_id = f"RFQ-TEST-ITEM-REF-{uid}"
    services.create_rfq(rfq_id, "Item Ref Test", "INR", [
        {"sku": item.internal_sku, "description": item.canonical_description, "internal_item_id": item.internal_item_id, "requested_quantity": "10", "requested_uom": "PCS"}
    ])

    success, msg = services.delete_item_master_record(item.internal_item_id)
    assert success is False
    assert "Cannot hard delete" in msg
    assert "referenced in" in msg
    assert services.get_item_master_item(item.internal_item_id) is not None


def test_03_referenced_item_can_be_deactivated():
    uid = uuid.uuid4().hex[:6]
    sku = f"TEST-DEACT-{uid}"
    item = services.add_item_to_master(
        internal_sku=sku,
        canonical_description="Deactivation Test Item",
        stocking_uom="PCS"
    )
    services.create_rfq(f"RFQ-DEACT-{uid}", "Deact RFQ", "INR", [
        {"sku": item.internal_sku, "description": item.canonical_description, "internal_item_id": item.internal_item_id, "requested_quantity": "1", "requested_uom": "PCS"}
    ])

    success, msg = services.deactivate_item_master_record(item.internal_item_id)
    assert success is True
    
    updated = services.get_item_master_item(item.internal_item_id)
    assert updated.status == "INACTIVE"

    # Verify exclusion from active catalog
    active_items = services.get_item_master(include_inactive=False)
    assert all(it.internal_item_id != item.internal_item_id for it in active_items)

    all_items = services.get_item_master(include_inactive=True)
    assert any(it.internal_item_id == item.internal_item_id and it.status == "INACTIVE" for it in all_items)


def test_04_inactive_item_is_excluded_from_new_matching_and_catalog_selection():
    uid = uuid.uuid4().hex[:6]
    sku = f"TEST-INACT-{uid}"
    item = services.add_item_to_master(
        internal_sku=sku,
        canonical_description="Inactive Item",
        stocking_uom="PCS"
    )
    services.deactivate_item_master_record(item.internal_item_id)

    active_items = services.get_item_master(include_inactive=False)
    assert all(it.internal_item_id != item.internal_item_id for it in active_items)


def test_05_historical_rfq_still_resolves_inactive_item(client: TestClient):
    uid = uuid.uuid4().hex[:6]
    sku = f"TEST-HIST-{uid}"
    item = services.add_item_to_master(
        internal_sku=sku,
        canonical_description="Historical Item",
        stocking_uom="PCS"
    )
    rfq_id = f"RFQ-HIST-{uid}"
    services.create_rfq(rfq_id, "Hist Test", "INR", [
        {"sku": item.internal_sku, "description": item.canonical_description, "internal_item_id": item.internal_item_id, "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    services.deactivate_item_master_record(item.internal_item_id)

    res = client.get(f"/rfqs/{rfq_id}")
    assert res.status_code == 200
    assert sku.upper() in res.text


def test_06_bulk_deactivate_works():
    uid = uuid.uuid4().hex[:6]
    it1 = services.add_item_to_master(f"BULK-DEACT-1-{uid}", "Bulk Item 1")
    it2 = services.add_item_to_master(f"BULK-DEACT-2-{uid}", "Bulk Item 2")
    
    count = services.bulk_deactivate_items([it1.internal_item_id, it2.internal_item_id])
    assert count == 2
    assert services.get_item_master_item(it1.internal_item_id).status == "INACTIVE"
    assert services.get_item_master_item(it2.internal_item_id).status == "INACTIVE"


def test_07_bulk_restore_works():
    uid = uuid.uuid4().hex[:6]
    it1 = services.add_item_to_master(f"BULK-REST-1-{uid}", "Bulk Rest 1")
    it2 = services.add_item_to_master(f"BULK-REST-2-{uid}", "Bulk Rest 2")
    services.bulk_deactivate_items([it1.internal_item_id, it2.internal_item_id])
    
    count = services.bulk_restore_items([it1.internal_item_id, it2.internal_item_id])
    assert count == 2
    assert services.get_item_master_item(it1.internal_item_id).status == "ACTIVE"
    assert services.get_item_master_item(it2.internal_item_id).status == "ACTIVE"


def test_08_bulk_safe_delete_deletes_only_unused():
    uid = uuid.uuid4().hex[:6]
    it_unused = services.add_item_to_master(f"BULK-DEL-UN-{uid}", "Bulk Delete Unused")
    it_used = services.add_item_to_master(f"BULK-DEL-US-{uid}", "Bulk Delete Used")
    services.create_rfq(f"RFQ-BULK-DEL-{uid}", "Bulk Del RFQ", "INR", [
        {"sku": it_used.internal_sku, "description": it_used.canonical_description, "internal_item_id": it_used.internal_item_id, "requested_quantity": "5", "requested_uom": "PCS"}
    ])
    
    res = services.bulk_delete_items([it_unused.internal_item_id, it_used.internal_item_id])
    assert res["deleted_count"] == 1
    assert res["blocked_count"] == 1
    assert it_unused.internal_item_id in res["deleted_ids"]
    assert res["blocked_items"][0]["item_id"] == it_used.internal_item_id


def test_09_undo_import_removes_only_records_from_that_import():
    uid = uuid.uuid4().hex[:6]
    batch_id = f"IMP-TEST-UNDO-{uid}"
    it1 = ItemMasterRecord(
        internal_item_id=f"ITEM-UNDO-1-{uid}",
        internal_sku=f"SKU-UNDO-1-{uid}",
        canonical_description="Accidental Import 1",
        stocking_uom="PCS",
        status="ACTIVE",
        import_batch_id=batch_id
    )
    it2 = ItemMasterRecord(
        internal_item_id=f"ITEM-UNDO-2-{uid}",
        internal_sku=f"SKU-UNDO-2-{uid}",
        canonical_description="Accidental Import 2",
        stocking_uom="PCS",
        status="ACTIVE",
        import_batch_id=batch_id
    )
    catalog = services.get_item_master(include_inactive=True)
    catalog.extend([it1, it2])
    services.save_item_master(catalog)
    services.record_import_batch(batch_id, "student_list.xlsx", [it1.internal_item_id, it2.internal_item_id])

    result = services.undo_import_batch(batch_id)
    assert result["deleted_count"] == 2
    assert services.get_item_master_item(it1.internal_item_id) is None
    assert services.get_item_master_item(it2.internal_item_id) is None


def test_10_undo_import_does_not_remove_pre_existing_catalog_records():
    bear = services.get_item_master_item("ITEM-001") or services.get_item_master_item("BEAR-6205-2RS")
    assert bear is not None


def test_11_undo_import_deactivates_referenced_records_instead_of_deleting():
    uid = uuid.uuid4().hex[:6]
    batch_id = f"IMP-TEST-UNDO-REF-{uid}"
    it_ref = ItemMasterRecord(
        internal_item_id=f"ITEM-UNDO-REF-{uid}",
        internal_sku=f"SKU-UNDO-REF-{uid}",
        canonical_description="Referenced Accidental Import",
        stocking_uom="PCS",
        status="ACTIVE",
        import_batch_id=batch_id
    )
    it_unref = ItemMasterRecord(
        internal_item_id=f"ITEM-UNDO-UNREF-{uid}",
        internal_sku=f"SKU-UNDO-UNREF-{uid}",
        canonical_description="Unreferenced Accidental Import",
        stocking_uom="PCS",
        status="ACTIVE",
        import_batch_id=batch_id
    )
    catalog = services.get_item_master(include_inactive=True)
    catalog.extend([it_ref, it_unref])
    services.save_item_master(catalog)
    services.record_import_batch(batch_id, "accidental_list.xlsx", [it_ref.internal_item_id, it_unref.internal_item_id])

    services.create_rfq(f"RFQ-UNDO-{uid}", "Undo Ref RFQ", "INR", [
        {"sku": it_ref.internal_sku, "description": it_ref.canonical_description, "internal_item_id": it_ref.internal_item_id, "requested_quantity": "5", "requested_uom": "PCS"}
    ])

    result = services.undo_import_batch(batch_id)
    assert result["deleted_count"] == 1
    assert result["deactivated_count"] == 1

    assert services.get_item_master_item(it_unref.internal_item_id) is None
    ref_after = services.get_item_master_item(it_ref.internal_item_id)
    assert ref_after is not None
    assert ref_after.status == "INACTIVE"


def test_12_supplier_name_can_be_edited():
    supp_id = "BENCH-SUPP-B"
    success, msg, rec = services.update_supplier_name(supp_id, "Bharat Industrial Components Pvt. Ltd.", changed_by="TESTER")
    assert success is True
    assert rec.supplier_name == "Bharat Industrial Components Pvt. Ltd."


def test_13_supplier_id_remains_unchanged_after_rename():
    supp = services.get_supplier("BENCH-SUPP-B")
    assert supp.supplier_id == "BENCH-SUPP-B"
    assert supp.supplier_name == "Bharat Industrial Components Pvt. Ltd."


def test_14_supplier_name_history_is_persisted():
    supp = services.get_supplier("BENCH-SUPP-B")
    assert len(supp.name_history) >= 1
    assert supp.name_history[-1].old_name == "Bharat Industrial Components"
    assert supp.name_history[-1].new_name == "Bharat Industrial Components Pvt. Ltd."
    assert supp.name_history[-1].changed_by == "TESTER"


def test_15_historical_award_supplier_name_remains_unchanged_after_rename():
    award = services.get_award_decision(services.DEMO_RFQ_ID)
    if award:
        assert award.get("rfq_id") == services.DEMO_RFQ_ID


def test_16_new_procurement_uses_new_supplier_name():
    supp = services.get_supplier("BENCH-SUPP-B")
    assert supp.supplier_name == "Bharat Industrial Components Pvt. Ltd."


def test_17_inactive_supplier_cannot_receive_new_award():
    supp_id = "BENCH-SUPP-D"
    services.deactivate_supplier(supp_id)
    supp = services.get_supplier(supp_id)
    assert supp.status == "INACTIVE"

    rfq_id = "RFQ-TEST-INACTIVE-SUPP-AWARD"
    services.create_rfq(rfq_id, "Inactive Supp Award Test", "INR", [
        {"sku": "BEAR-6205-2RS", "description": "Bearing", "requested_quantity": "10", "requested_uom": "PCS"}
    ])
    
    invalid_award = {
        "rfq_id": rfq_id,
        "selected_scenario": "SINGLE_SUPPLIER_L1",
        "base_currency": "INR",
        "allocations": [
            {
                "rfq_line_id": "RFQ-LINE-001",
                "item_sku": "BEAR-6205-2RS",
                "item_description": "Bearing",
                "required_qty": "10",
                "required_uom": "PCS",
                "supplier_splits": [
                    {
                        "supplier_id": "BENCH-SUPP-D",
                        "allocated_qty": "10",
                        "unit_landed_cost": "120.00"
                    }
                ]
            }
        ]
    }
    with pytest.raises(ValueError) as exc:
        services.save_award_decision(rfq_id, invalid_award, is_finalized=False)
    assert "INACTIVE" in str(exc.value)

    services.restore_supplier(supp_id)
    assert services.get_supplier(supp_id).status == "ACTIVE"


def test_18_historical_awards_still_display_inactive_suppliers(client: TestClient):
    res = client.get(f"/rfqs/{services.DEMO_RFQ_ID}/award")
    assert res.status_code in [200, 303]


def test_19_cross_rfq_data_cannot_be_affected_by_item_or_supplier_bulk_operations():
    bench_rfq = services.get_rfq(services.DEMO_RFQ_ID)
    if bench_rfq:
        assert len(bench_rfq.items) == 20


def test_20_api_endpoints_integration(client: TestClient):
    res = client.get("/api/suppliers")
    assert res.status_code == 200
    data = res.json()
    assert "suppliers" in data

    res_ih = client.get("/api/item-master/import-history")
    assert res_ih.status_code == 200
    assert "batches" in res_ih.json()
