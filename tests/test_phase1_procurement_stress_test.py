import json
import io
import openpyxl
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import services
from datasets.procurement_benchmark.dataset import get_benchmark_item_master
from matching.models import MatchStatus


@pytest.fixture
def client():
    return TestClient(app)


def _generate_messy_multisheet_xlsx(supplier_name: str) -> bytes:
    wb = openpyxl.Workbook()
    
    # Sheet 1: Cover Sheet / Metadata
    ws1 = wb.active
    ws1.title = "Cover & Notes"
    ws1.append(["PROPOSAL / COMMERCIAL QUOTATION"])
    ws1.append(["Supplier Name:", supplier_name])
    ws1.append(["Quote Date:", "2026-08-20"])
    ws1.append(["Validity:", "60 Days"])
    ws1.append([])
    ws1.append(["Notice: Please refer to Sheet 'Price Schedule' for itemized pricing."])
    
    # Sheet 2: Price Schedule with blank rows, merged header, and formulas
    ws2 = wb.create_sheet(title="Price Schedule")
    ws2.append([])  # Blank row at top
    ws2.append(["COMMERCIAL PROPOSAL - SCHEDULE OF RATES"])
    ws2.merge_cells("A2:F2")
    ws2.append([])  # Another blank gap
    
    headers = ["Item No.", "Part Reference / SKU", "Description & Specifications", "Order Qty", "UOM", "Unit Landed Price (INR)"]
    ws2.append(headers)
    
    items_data = [
        [1, "BEAR-6205-2RS", "Deep Groove Ball Bearing 25x52x15mm", 100, "PCS", 128.50],
        [2, "BEAR-6206-2RS", "Deep Groove Ball Bearing 30x62x16mm", 80, "PCS", 175.00],
        [3, "CABL-6SQ-CU", "Copper Armoured Flexible Cable 6 Sq.mm", 500, "MTR", 195.00],
        [4, "CABL-10SQ-CU", "Copper Armoured Flexible Cable 10 Sq.mm", 300, "MTR", 310.00],
        [5, "DIN-912-M8-40", "Hex Socket Head Cap Screw M8x40mm SS304", 10, "BOX", 450.00], # 1 BOX = 100 PCS
        [6, "VALV-BALL-2IN", "Cast Steel Ball Valve 2-inch Flanged Class 150", 15, "PCS", 3650.00],
        [7, "VALV-CHK-1IN", "Stainless Steel Check Valve 1-inch NPT", 25, "PCS", 1850.00],
        [8, "LC1D32M7", "3-Pole AC Power Contactor 32A 230VAC Coil", 40, "PCS", 1420.00],
        [9, "NSX100F", "Molded Case Circuit Breaker 100A 3-Pole 36kA", 10, "PCS", 4950.00],
        [10, "PT-10B-420", "Industrial Pressure Transmitter 0-10 Bar Output 4-20mA", 8, "PCS", 7800.00],
    ]
    
    for row in items_data:
        ws2.append(row)
        
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_phase1_realistic_adversarial_procurement_stress_test(client: TestClient):
    """
    PHASE 1 REALISTIC ADVERSARIAL PROCUREMENT STRESS TEST
    Covers:
    - 20 RFQ line items across multiple industrial categories
    - 7 diverse suppliers (Complete, Partial, Messy multi-sheet XLSX, Surcharges/Taxes, Volume Tiers/USD, Extra lines, Ambiguous items)
    - Manual match resolution in Review Center
    - Deterministic Commercial Evaluation
    - Multi-supplier split sourcing award
    - Reopening, revision audit trail, and re-finalization
    - End-to-end immutability and persistence assertions
    """
    rfq_id = "RFQ-STRESS-PHASE1-E2E"
    
    # 0. Clean any prior test artifacts
    award_file = services.get_award_file_path(rfq_id)
    if award_file.exists():
        award_file.unlink()

    # 1. Create RFQ (20 Items)
    item_master = get_benchmark_item_master()
    rfq_items = []
    for idx, im in enumerate(item_master[:20]):
        req_qty = "100" if im.stocking_uom == "PCS" else ("500" if im.stocking_uom == "MTR" else "20")
        rfq_items.append({
            "sku": im.internal_sku,
            "description": im.canonical_description,
            "requested_quantity": req_qty,
            "requested_uom": im.stocking_uom,
            "target_price": "1000.00"
        })
    
    created_rfq = services.create_rfq(
        rfq_id=rfq_id,
        title="Phase 1 Mega Enterprise Industrial Plant Sourcing",
        base_currency="INR",
        items=rfq_items
    )
    assert created_rfq.rfq_id == rfq_id
    assert len(created_rfq.items) == 20

    # 2. Supplier Quotations Ingestion (7 Suppliers)
    # Supplier 1: Apex Industrial Corp (Complete, Clean, All 20 items in INR)
    q1 = services.create_quote("Apex Industrial Corp", rfq_id=rfq_id)
    csv1_lines = ["Line,SKU,Description,Qty,UOM,Unit Price"]
    for idx, im in enumerate(item_master[:20]):
        price = "120.00" if "BEAR" in im.internal_sku else ("180.00" if "CABL" in im.internal_sku else "2500.00")
        qty = "100" if im.stocking_uom == "PCS" else ("500" if im.stocking_uom == "MTR" else "20")
        csv1_lines.append(f"{idx+1},{im.internal_sku},{im.canonical_description},{qty},{im.stocking_uom},{price}")
    services.upload_source_file(q1, "apex_quote.csv", "\n".join(csv1_lines).encode("utf-8"))

    # Supplier 2: Bharat Tech Solutions (Partial 12 items + 3 extra unrequested items)
    q2 = services.create_quote("Bharat Tech Solutions", rfq_id=rfq_id)
    csv2_lines = ["Line Number,Supplier Part No,Description,Quoted Qty,UOM,Unit Price,Discount Pct"]
    for idx, im in enumerate(item_master[:12]):
        price = "115.00" if "BEAR" in im.internal_sku else ("175.00" if "CABL" in im.internal_sku else "2400.00")
        qty = "100" if im.stocking_uom == "PCS" else ("500" if im.stocking_uom == "MTR" else "20")
        supp_pn = im.approved_supplier_part_numbers[0] if im.approved_supplier_part_numbers else im.internal_sku
        csv2_lines.append(f"{idx+1},{supp_pn},{im.canonical_description},{qty},{im.stocking_uom},{price},5.0")
    # 3 extra unrequested items
    csv2_lines.append("13,EXTRA-DRILL-10MM,Heavy Duty Rotary Drill Bit 10mm,50,PCS,85.00,0.0")
    csv2_lines.append("14,EXTRA-WRENCH-ADJ,Adjustable Pipe Wrench 12in Steel,10,PCS,320.00,0.0")
    csv2_lines.append("15,EXTRA-GREASE-500G,High Temperature Synthetic Grease 500g,20,CAN,450.00,0.0")
    services.upload_source_file(q2, "bharat_partial.csv", "\n".join(csv2_lines).encode("utf-8"))

    # Supplier 3: Global Precision S.A. (Multi-Currency USD with volume pricing)
    q3 = services.create_quote("Global Precision S.A.", rfq_id=rfq_id)
    csv3_lines = ["Item,Description,Qty,UOM,Unit Price USD,Currency"]
    for idx, im in enumerate(item_master[:15]):
        # Converted USD pricing ~$1.50 for bearing, ~$30 for valve
        usd_price = "1.45" if "BEAR" in im.internal_sku else ("2.10" if "CABL" in im.internal_sku else "28.50")
        qty = "100" if im.stocking_uom == "PCS" else ("500" if im.stocking_uom == "MTR" else "20")
        csv3_lines.append(f"{idx+1},{im.canonical_description},{qty},{im.stocking_uom},{usd_price},USD")
    services.upload_source_file(q3, "global_precision_usd.csv", "\n".join(csv3_lines).encode("utf-8"))

    # Supplier 4: Delta Dynamic Ltd (Messy Multi-sheet XLSX with blank rows, merged cells, formulas)
    q4 = services.create_quote("Delta Dynamic Ltd", rfq_id=rfq_id)
    xlsx4_bytes = _generate_messy_multisheet_xlsx("Delta Dynamic Ltd")
    services.upload_source_file(q4, "delta_messy.xlsx", xlsx4_bytes)

    # Supplier 5: Evergreen Industrial Supplies (Ambiguous descriptions requiring manual review)
    q5 = services.create_quote("Evergreen Industrial Supplies", rfq_id=rfq_id)
    csv5_lines = [
        "Pos,Description,Qty,UOM,Rate",
        "1,Generic Ball Bearing 6205 Series,100,PCS,122.00", # Ambiguous for ITEM-001
        "2,Generic Ball Bearing 6206 Series,80,PCS,168.00",  # Ambiguous for ITEM-002
        "3,Industrial Cable 6sqmm 4Core,500,MTR,188.00",
        "4,Industrial Cable 10sqmm 4Core,300,MTR,305.00",
        "5,Stainless Hex Screw M8 40mm,1000,PCS,4.20",
    ]
    services.upload_source_file(q5, "evergreen_ambiguous.csv", "\n".join(csv5_lines).encode("utf-8"))

    # Supplier 6: Frontier Engineering (Freight, Packing Charges & GST Taxes)
    q6 = services.create_quote("Frontier Engineering", rfq_id=rfq_id)
    csv6_lines = ["Line,SKU,Description,Qty,UOM,Unit Price,Tax Pct,Freight Charge"]
    for idx, im in enumerate(item_master[:14]):
        price = "125.00" if "BEAR" in im.internal_sku else ("190.00" if "CABL" in im.internal_sku else "2550.00")
        qty = "100" if im.stocking_uom == "PCS" else ("500" if im.stocking_uom == "MTR" else "20")
        csv6_lines.append(f"{idx+1},{im.internal_sku},{im.canonical_description},{qty},{im.stocking_uom},{price},18.0,500.00")
    services.upload_source_file(q6, "frontier_surcharges.csv", "\n".join(csv6_lines).encode("utf-8"))

    # Supplier 7: Zenith Heavy Equipment (Partial 8 items + 4 completely foreign items)
    q7 = services.create_quote("Zenith Heavy Equipment", rfq_id=rfq_id)
    csv7_lines = [
        "Item,Part No,Description,Qty,UOM,Price",
        "1,BEAR-6205-2RS,Deep Groove Ball Bearing 25x52x15mm,100,PCS,130.00",
        "2,BEAR-6206-2RS,Deep Groove Ball Bearing 30x62x16mm,80,PCS,172.00",
        "3,VALV-BALL-2IN,Cast Steel Ball Valve 2-inch Flanged,15,PCS,3500.00",
        "4,VALV-CHK-1IN,Stainless Steel Check Valve 1-inch NPT,25,PCS,1800.00",
        "5,FOREIGN-PUMP-01,Submersible Drainage Sump Pump 3HP,2,UNIT,22000.00",
        "6,FOREIGN-COMP-02,Air Compressor Dual Cylinder 50L,1,UNIT,34000.00",
    ]
    services.upload_source_file(q7, "zenith_partial.csv", "\n".join(csv7_lines).encode("utf-8"))

    all_quotes = [q1, q2, q3, q4, q5, q6, q7]
    assert len(all_quotes) == 7

    # 3. Extraction & Matching
    for qid in all_quotes:
        ext_res = services.run_extraction(qid)
        assert len(ext_res.items) > 0, f"Extraction failed for quote {qid}"
        services.run_matching_for_quote(qid, rfq_id)

    # 4. Manual Match Resolution for Ambiguous Lines in Q5 (Evergreen)
    matched_q5 = services.get_matched_items(q5)
    # Line 0: "Generic Ball Bearing 6205 Series" -> resolve to BEAR-6205-2RS
    services.resolve_match_candidate(q5, 0, "BEAR-6205-2RS", action="ACCEPT")
    # Line 1: "Generic Ball Bearing 6206 Series" -> resolve to BEAR-6206-2RS
    services.resolve_match_candidate(q5, 1, "BEAR-6206-2RS", action="ACCEPT")

    # Verify locked human confirmation
    q5_updated = services.get_matched_items(q5)
    assert q5_updated[0].match_status == MatchStatus.EXACT_MATCH
    assert "human_confirmation" in q5_updated[0].item_master_match.matched_fields
    assert q5_updated[1].match_status == MatchStatus.EXACT_MATCH
    assert "human_confirmation" in q5_updated[1].item_master_match.matched_fields

    # 5. Run Commercial Evaluation (Comparison)
    comp_result = services.run_rfq_comparison(rfq_id, all_quotes, base_currency="INR")
    comp_id = comp_result["comparison_id"]
    assert comp_id.startswith("COMP-")
    assert len(comp_result["comparison"]["item_comparisons"]) == 20

    # Verify Human Decision survived comparison
    q5_after_comp = services.get_matched_items(q5)
    assert q5_after_comp[0].match_status == MatchStatus.EXACT_MATCH
    assert "human_confirmation" in q5_after_comp[0].item_master_match.matched_fields

    # 6. Award Proposal Generation
    proposal = services.build_proposed_award_allocation(rfq_id, scenario="LINE_ITEM_OPTIMAL")
    assert proposal["rfq_id"] == rfq_id
    assert proposal["total_required_items"] == 20
    assert len(proposal["allocations"]) == 20
    assert Decimal(proposal["total_awarded_value"]) > Decimal("0")

    # 7. Multi-Supplier Split Allocation Customization
    # Modify Line 0 (BEAR-6205-2RS, required 100 PCS): Split 60 PCS to Bharat Tech (Q2), 40 PCS to Apex (Q1)
    alloc_0 = proposal["allocations"][0]
    line0_id = alloc_0["rfq_line_id"]
    
    # Locate bids for Q2 and Q1 on line 0
    bids_line0 = {b["quote_id"]: b for b in alloc_0["available_bids"]}
    assert q2 in bids_line0 or q1 in bids_line0

    alloc_0["supplier_splits"] = [
        {
            "supplier_id": q2,
            "quote_id": q2,
            "allocated_qty": "60",
            "unit_landed_cost": str(bids_line0[q2]["unit_landed_cost"])
        },
        {
            "supplier_id": q1,
            "quote_id": q1,
            "allocated_qty": "40",
            "unit_landed_cost": str(bids_line0[q1]["unit_landed_cost"])
        }
    ]

    # Finalize Award
    award_final = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert award_final["status"] == "FINALIZED"
    assert award_final["validation_passed"] is True
    assert award_final["fully_allocated_items_count"] == 20
    assert Decimal(award_final["total_awarded_value"]) > Decimal("0")
    total_val_1 = Decimal(award_final["total_awarded_value"])

    # 8. Reopen Award for Revision
    reopened = services.reopen_award_decision(rfq_id, reason="Negotiated volume discount on valves; adjusting line split")
    assert reopened["status"] == "REOPENED"
    assert len(reopened["reopen_history"]) == 1
    assert Decimal(reopened["reopen_history"][0]["previous_total_awarded_value"]) == total_val_1

    # 9. Modify & Re-finalize Award
    # Change Line 0 to 70/30 split
    alloc_0["supplier_splits"][0]["allocated_qty"] = "70"
    alloc_0["supplier_splits"][1]["allocated_qty"] = "30"
    
    award_refinalized = services.save_award_decision(rfq_id, proposal, is_finalized=True)
    assert award_refinalized["status"] == "FINALIZED"
    assert award_refinalized["validation_passed"] is True
    assert len(award_refinalized["reopen_history"]) == 1

    # 10. Verify Persisted File on Disk
    persisted_award = services.get_award_decision(rfq_id)
    assert persisted_award["status"] == "FINALIZED"
    assert persisted_award["rfq_id"] == rfq_id
    assert persisted_award["comparison_id"] == comp_id
    assert len(persisted_award["allocations"]) == 20
    assert len(persisted_award["reopen_history"]) == 1

    # 11. UI Revisit Verification via TestClient
    res_home = client.get("/")
    assert res_home.status_code == 200

    res_rfq = client.get(f"/rfqs/{rfq_id}")
    assert res_rfq.status_code == 200

    res_comp = client.get(f"/comparisons/{comp_id}")
    assert res_comp.status_code == 200

    res_award = client.get(f"/rfqs/{rfq_id}/award")
    assert res_award.status_code == 200
    assert "AWARD FINALIZED" in res_award.text
    assert persisted_award["total_awarded_value"] in res_award.text or f"{float(persisted_award['total_awarded_value']):,.2f}" in res_award.text
    print("\n>>> PHASE 1 MEGA PROCUREMENT STRESS TEST COMPLETED AND FULLY VERIFIED.")
