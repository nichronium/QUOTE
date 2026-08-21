"""
Controlled Procurement Benchmark Dataset.
Provides:
1. 20-item RFQ across 6 product categories (Bearings, Cables, Fasteners, Valves, Electricals/Sensors, PPE/Hoses).
2. Complete Item Master with exact internal SKUs, manufacturer PNs, approved supplier SKUs, and specifications.
3. Generator for 5 realistic Supplier XLSX quotations with deliberate variations (clean, alternative headers, UOM conversion, volume tiers in USD, incomplete/provisional).
4. Explicit Ground Truth definitions for Phase 1 (extraction), Phase 2 (matching), Phase 3 (comparison & ranking), and Split Sourcing.
"""

from decimal import Decimal
import io
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import openpyxl

from core.canonical_quote import PriceTier
from matching.models import ItemMasterRecord, MatchStatus, RFQLineItem
from comparison.models import RFQDocument


# =========================================================================
# 1. ITEM MASTER (20 PRODUCTS)
# =========================================================================

def get_benchmark_item_master() -> List[ItemMasterRecord]:
    return [
        ItemMasterRecord(
            internal_item_id="ITEM-001",
            internal_sku="BEAR-6205-2RS",
            manufacturer_part_number="6205-2RS1",
            approved_supplier_part_numbers=["SKF-6205-2RS", "FAG-6205-2RSR", "6205.2RS"],
            canonical_description="Deep Groove Ball Bearing 25x52x15mm Rubber Sealed",
            stocking_uom="PCS",
            brand="SKF",
            specifications={"size": "25x52x15mm", "type": "Ball Bearing"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-002",
            internal_sku="BEAR-6206-2RS",
            manufacturer_part_number="6206-2RS1",
            approved_supplier_part_numbers=["SKF-6206-2RS", "FAG-6206-2RSR", "6206.2RS"],
            canonical_description="Deep Groove Ball Bearing 30x62x16mm Rubber Sealed",
            stocking_uom="PCS",
            brand="SKF",
            specifications={"size": "30x62x16mm", "type": "Ball Bearing"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-003",
            internal_sku="CABL-6SQ-CU",
            manufacturer_part_number="CAB-6SQ-FLEX",
            approved_supplier_part_numbers=["POL-6SQ-CU", "HAV-6SQ-4C", "FIN-6SQ-CU"],
            canonical_description="Copper Armoured Flexible Cable 6 Sq.mm 4-Core",
            stocking_uom="MTR",
            brand="Polycab",
            specifications={"size": "6 sq.mm", "cores": "4"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-004",
            internal_sku="CABL-10SQ-CU",
            manufacturer_part_number="CAB-10SQ-FLEX",
            approved_supplier_part_numbers=["POL-10SQ-CU", "HAV-10SQ-4C", "FIN-10SQ-CU"],
            canonical_description="Copper Armoured Flexible Cable 10 Sq.mm 4-Core",
            stocking_uom="MTR",
            brand="Polycab",
            specifications={"size": "10 sq.mm", "cores": "4"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-005",
            internal_sku="FAST-SS-M8-40",
            manufacturer_part_number="DIN-912-M8-40",
            approved_supplier_part_numbers=["SS-M8-40", "BOLT-M8X40-SS"],
            canonical_description="Hex Socket Head Cap Screw M8x40mm Stainless Steel 304",
            stocking_uom="PCS",
            approved_conversion_factors={"BOX": Decimal("100.0")},
            brand="Unbrako",
            specifications={"size": "M8x40mm", "grade": "SS304"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-006",
            internal_sku="FAST-SS-M8-50",
            manufacturer_part_number="DIN-912-M8-50",
            approved_supplier_part_numbers=["SS-M8-50", "BOLT-M8X50-SS"],
            canonical_description="Hex Socket Head Cap Screw M8x50mm Stainless Steel 304",
            stocking_uom="PCS",
            approved_conversion_factors={"BOX": Decimal("100.0")},
            brand="Unbrako",
            specifications={"size": "M8x50mm", "grade": "SS304"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-007",
            internal_sku="FAST-HEX-M12-60",
            manufacturer_part_number="ISO-4014-M12-60",
            approved_supplier_part_numbers=["UNB-M12-60", "BOLT-M12X60-8.8"],
            canonical_description="High Tensile Hex Bolt M12x60mm Grade 8.8 Galvanized",
            stocking_uom="PCS",
            brand="TVS",
            specifications={"size": "M12x60mm", "grade": "Grade 8.8"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-008",
            internal_sku="FAST-NUT-M12",
            manufacturer_part_number="DIN-934-M12",
            approved_supplier_part_numbers=["UNB-NUT-M12", "NUT-M12-8.8"],
            canonical_description="High Tensile Hex Nut M12 Grade 8.8 Galvanized",
            stocking_uom="PCS",
            brand="TVS",
            specifications={"size": "M12", "grade": "Grade 8.8"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-009",
            internal_sku="VALV-BALL-2IN",
            manufacturer_part_number="BV-150-2IN",
            approved_supplier_part_numbers=["LNT-BV-2IN", "AUD-BV-50"],
            canonical_description="Cast Steel Ball Valve 2-inch Flanged Class 150",
            stocking_uom="PCS",
            brand="L&T",
            specifications={"size": "2-inch", "class": "150#"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-010",
            internal_sku="VALV-CHK-1IN",
            manufacturer_part_number="CV-NPT-1IN",
            approved_supplier_part_numbers=["LNT-CV-1IN", "AUD-CV-25"],
            canonical_description="Stainless Steel Check Valve 1-inch NPT Threaded",
            stocking_uom="PCS",
            brand="L&T",
            specifications={"size": "1-inch", "type": "Check Valve"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-011",
            internal_sku="ELEC-CONT-32A",
            manufacturer_part_number="LC1D32M7",
            approved_supplier_part_numbers=["SCH-LC1D32", "ABB-AF30"],
            canonical_description="3-Pole AC Power Contactor 32A 230VAC Coil",
            stocking_uom="PCS",
            brand="Schneider",
            specifications={"rating": "32A", "coil": "230VAC"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-012",
            internal_sku="ELEC-MCCB-100A",
            manufacturer_part_number="NSX100F",
            approved_supplier_part_numbers=["SCH-NSX100", "ABB-XT1N100"],
            canonical_description="Molded Case Circuit Breaker 100A 3-Pole 36kA",
            stocking_uom="PCS",
            brand="Schneider",
            specifications={"rating": "100A", "poles": "3-Pole"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-013",
            internal_sku="INST-PT-10BAR",
            manufacturer_part_number="PT-10B-420",
            approved_supplier_part_numbers=["YOK-PT-10B", "HON-PT-10"],
            canonical_description="Industrial Pressure Transmitter 0-10 Bar Output 4-20mA",
            stocking_uom="PCS",
            brand="Yokogawa",
            specifications={"range": "0-10 Bar", "output": "4-20mA"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-014",
            internal_sku="INST-RTD-PT100",
            manufacturer_part_number="RTD-PT100-100",
            approved_supplier_part_numbers=["RAD-PT100-100", "WIK-PT100"],
            canonical_description="RTD Temperature Sensor Pt100 100mm Probe Length",
            stocking_uom="PCS",
            brand="Radix",
            specifications={"element": "Pt100", "length": "100mm"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-015",
            internal_sku="MTR-IND-5.5KW",
            manufacturer_part_number="1LA7130-4AA",
            approved_supplier_part_numbers=["SIE-5.5KW-4P", "BBL-5.5KW"],
            canonical_description="3-Phase Induction Motor 5.5kW 4-Pole 415V Foot Mounted",
            stocking_uom="PCS",
            brand="Siemens",
            specifications={"power": "5.5kW", "poles": "4-Pole"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-016",
            internal_sku="VFD-5.5KW-400V",
            manufacturer_part_number="ACS580-01-012A-4",
            approved_supplier_part_numbers=["ABB-ACS580-5.5", "DAN-FC302-5.5"],
            canonical_description="Variable Frequency Drive 5.5kW 400V 3-Phase IP21",
            stocking_uom="PCS",
            brand="ABB",
            specifications={"power": "5.5kW", "voltage": "400V"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-017",
            internal_sku="GSK-SW-2IN-150",
            manufacturer_part_number="SWG-SS-2IN-150",
            approved_supplier_part_numbers=["KLN-SWG-2IN", "CHM-SWG-50"],
            canonical_description="Spiral Wound Gasket 2-inch Class 150 SS316 Graphite Filler",
            stocking_uom="PCS",
            brand="Klinger",
            specifications={"size": "2-inch", "rating": "150#"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-018",
            internal_sku="HOSE-HYD-0.5IN",
            manufacturer_part_number="SAE-100R2-0.5",
            approved_supplier_part_numbers=["PARK-100R2-0.5", "EAT-100R2-0.5"],
            canonical_description="High Pressure Hydraulic Hose 1/2-inch 2-Wire Braid 100R2",
            stocking_uom="MTR",
            brand="Parker",
            specifications={"size": "1/2-inch", "type": "100R2"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-019",
            internal_sku="PPE-GLV-SZ10",
            manufacturer_part_number="GLV-HD-SZ10",
            approved_supplier_part_numbers=["ANS-GLV-10", "HON-GLV-10"],
            canonical_description="Heavy Duty Chemical Resistant Nitrile Gloves Size 10",
            stocking_uom="PAIR",
            brand="Ansell",
            specifications={"size": "Size 10", "material": "Nitrile"}
        ),
        ItemMasterRecord(
            internal_item_id="ITEM-020",
            internal_sku="PPE-HLM-WHT",
            manufacturer_part_number="HLM-VGUARD-WHT",
            approved_supplier_part_numbers=["MSA-HLM-WHT", "KAR-HLM-WHT"],
            canonical_description="Industrial Safety Helmet with Ratchet Suspension White",
            stocking_uom="PCS",
            brand="MSA",
            specifications={"color": "White", "type": "V-Gard"}
        ),
    ]


# =========================================================================
# 2. RFQ DOCUMENT (20 ITEMS)
# =========================================================================

def get_benchmark_rfq() -> RFQDocument:
    master = get_benchmark_item_master()
    quantities = [
        Decimal("100.0"),  # ITEM-001 Bearings 6205
        Decimal("50.0"),   # ITEM-002 Bearings 6206
        Decimal("1000.0"), # ITEM-003 Cable 6sq
        Decimal("500.0"),  # ITEM-004 Cable 10sq
        Decimal("2000.0"), # ITEM-005 Screws M8x40
        Decimal("1500.0"), # ITEM-006 Screws M8x50
        Decimal("800.0"),  # ITEM-007 Bolts M12x60
        Decimal("1600.0"), # ITEM-008 Nuts M12
        Decimal("20.0"),   # ITEM-009 Ball Valve 2in
        Decimal("30.0"),   # ITEM-010 Check Valve 1in
        Decimal("40.0"),   # ITEM-011 Contactor 32A
        Decimal("15.0"),   # ITEM-012 MCCB 100A
        Decimal("10.0"),   # ITEM-013 Pressure Transmitter
        Decimal("25.0"),   # ITEM-014 RTD Pt100
        Decimal("5.0"),    # ITEM-015 Motor 5.5kW
        Decimal("5.0"),    # ITEM-016 VFD 5.5kW
        Decimal("100.0"),  # ITEM-017 Gaskets 2in
        Decimal("200.0"),  # ITEM-018 Hydraulic Hose 0.5in
        Decimal("50.0"),   # ITEM-019 Gloves Sz10
        Decimal("60.0"),   # ITEM-020 Helmet White
    ]

    items = []
    for idx, (rec, qty) in enumerate(zip(master, quantities), start=1):
        items.append(RFQLineItem(
            rfq_line_id=f"RFQ-ITEM-{idx:02d}",
            internal_item_id=rec.internal_item_id,
            sku=rec.internal_sku,
            description=rec.canonical_description,
            requested_quantity=qty,
            requested_uom=rec.stocking_uom
        ))

    return RFQDocument(
        rfq_id="RFQ-2026-E2E-BENCHMARK",
        title="Industrial Plant Operations Annual Procurement RFQ",
        base_currency="INR",
        items=items
    )


# =========================================================================
# 3. SUPPLIER WORKBOOK GENERATOR
# =========================================================================

def generate_benchmark_workbooks(target_dir: Path) -> Dict[str, Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    # ---------------------------------------------------------------------
    # SUPPLIER A: Clean XLSX, Exact Supplier SKUs, INR, 18% GST
    # ---------------------------------------------------------------------
    wb_a = openpyxl.Workbook()
    ws_a = wb_a.active
    ws_a.title = "Quotation"
    ws_a.append(["Vendor: Alpha Industrial Supplies Ltd", "", "", "", "", ""])
    ws_a.append(["Quote Ref: AIS-2026-8801", "", "", "", "", ""])
    ws_a.append(["Payment Terms: 30 Days Net", "", "", "", "", ""])
    ws_a.append(["Delivery Terms: Ex-Works (Lead time: 10 days)", "", "", "", "", ""])
    ws_a.append([])
    ws_a.append(["Supplier Part Number", "Description", "Quantity", "UOM", "Unit Price", "GST Rate %"])

    supp_a_data = [
        ("SKF-6205-2RS", "Ball Bearing 25x52x15mm", 100, "PCS", 120.0, 18.0),
        ("SKF-6206-2RS", "Ball Bearing 30x62x16mm", 50, "PCS", 165.0, 18.0),
        ("POL-6SQ-CU", "Copper Armoured Cable 6 Sq.mm", 1000, "MTR", 240.0, 18.0),
        ("POL-10SQ-CU", "Copper Armoured Cable 10 Sq.mm", 500, "MTR", 380.0, 18.0),
        ("SS-M8-40", "Hex Socket Screw M8x40mm SS304", 2000, "PCS", 8.50, 18.0),
        ("SS-M8-50", "Hex Socket Screw M8x50mm SS304", 1500, "PCS", 10.20, 18.0),
        ("UNB-M12-60", "Hex Bolt M12x60mm Grade 8.8", 800, "PCS", 18.00, 18.0),
        ("UNB-NUT-M12", "Hex Nut M12 Grade 8.8", 1600, "PCS", 6.50, 18.0),
        ("LNT-BV-2IN", "Ball Valve 2-inch Class 150", 20, "PCS", 4800.0, 18.0),
        ("LNT-CV-1IN", "Check Valve 1-inch NPT", 30, "PCS", 2200.0, 18.0),
        ("SCH-LC1D32", "AC Power Contactor 32A 230VAC", 40, "PCS", 1650.0, 18.0),
        ("SCH-NSX100", "MCCB 100A 3-Pole 36kA", 15, "PCS", 6400.0, 18.0),
        ("YOK-PT-10B", "Pressure Transmitter 0-10 Bar", 10, "PCS", 18500.0, 18.0),
        ("RAD-PT100-100", "RTD Temperature Sensor Pt100", 25, "PCS", 1250.0, 18.0),
        ("SIE-5.5KW-4P", "Induction Motor 5.5kW 415V", 5, "PCS", 32000.0, 18.0),
        ("ABB-ACS580-5.5", "VFD 5.5kW 400V Drive", 5, "PCS", 42000.0, 18.0),
        ("KLN-SWG-2IN", "Spiral Wound Gasket 2-inch 150#", 100, "PCS", 185.0, 18.0),
        ("PARK-100R2-0.5", "Hydraulic Hose 1/2-inch 100R2", 200, "MTR", 420.0, 18.0),
        ("ANS-GLV-10", "Nitrile Safety Gloves Size 10", 50, "PAIR", 280.0, 18.0),
        ("MSA-HLM-WHT", "Safety Helmet White Ratchet", 60, "PCS", 320.0, 18.0),
    ]
    for row in supp_a_data:
        ws_a.append(list(row))

    path_a = target_dir / "supplier_a_alpha.xlsx"
    wb_a.save(path_a)
    paths["SUPP-A"] = path_a

    # ---------------------------------------------------------------------
    # SUPPLIER B: Varied columns, Manufacturer PNs, Split GST, Freight
    # ---------------------------------------------------------------------
    wb_b = openpyxl.Workbook()
    ws_b = wb_b.active
    ws_b.title = "Commercial_Offer"
    ws_b.append(["Supplier: Bharat Heavy Components Pvt Ltd", "", "", "", "", "", ""])
    ws_b.append(["Quotation Reference: BHC/QT/2026/094", "", "", "", "", "", ""])
    ws_b.append(["Terms: Net 30 Days Credit | Delivery: 14 Days", "", "", "", "", "", ""])
    ws_b.append([])
    ws_b.append(["Item Code / Mfg PN", "Item Specification", "Qty Quoted", "Units", "Rate (INR)", "CGST %", "SGST %"])

    supp_b_data = [
        ("6205-2RS1", "Deep Groove Ball Bearing 25x52x15mm", 100, "PCS", 115.0, 9.0, 9.0),
        ("6206-2RS1", "Deep Groove Ball Bearing 30x62x16mm", 50, "PCS", 160.0, 9.0, 9.0),
        ("CAB-6SQ-FLEX", "Copper Armoured Flexible Cable 6 Sq.mm", 1000, "MTR", 245.0, 9.0, 9.0),
        ("CAB-10SQ-FLEX", "Copper Armoured Flexible Cable 10 Sq.mm", 500, "MTR", 390.0, 9.0, 9.0),
        ("DIN-912-M8-40", "Hex Socket Head Cap Screw M8x40mm", 2000, "PCS", 8.20, 9.0, 9.0),
        ("DIN-912-M8-50", "Hex Socket Head Cap Screw M8x50mm", 1500, "PCS", 9.80, 9.0, 9.0),
        ("ISO-4014-M12-60", "High Tensile Hex Bolt M12x60mm", 800, "PCS", 17.50, 9.0, 9.0),
        ("DIN-934-M12", "High Tensile Hex Nut M12", 1600, "PCS", 6.20, 9.0, 9.0),
        ("BV-150-2IN", "Cast Steel Ball Valve 2-inch Flanged", 20, "PCS", 4700.0, 9.0, 9.0),
        ("CV-NPT-1IN", "Stainless Steel Check Valve 1-inch", 30, "PCS", 2150.0, 9.0, 9.0),
        ("LC1D32M7", "3-Pole AC Contactor 32A 230VAC", 40, "PCS", 1620.0, 9.0, 9.0),
        ("NSX100F", "MCCB 100A 3-Pole 36kA", 15, "PCS", 6300.0, 9.0, 9.0),
        ("PT-10B-420", "Pressure Transmitter 0-10 Bar", 10, "PCS", 18200.0, 9.0, 9.0),
        ("RTD-PT100-100", "RTD Sensor Pt100 100mm", 25, "PCS", 1200.0, 9.0, 9.0),
        ("1LA7130-4AA", "Induction Motor 5.5kW 415V", 5, "PCS", 31500.0, 9.0, 9.0),
        ("ACS580-01-012A-4", "VFD 5.5kW 400V Inverter", 5, "PCS", 41500.0, 9.0, 9.0),
        ("SWG-SS-2IN-150", "Spiral Wound Gasket 2-inch 150#", 100, "PCS", 175.0, 9.0, 9.0),
        ("SAE-100R2-0.5", "Hydraulic Hose 1/2-inch 2-Wire", 200, "MTR", 410.0, 9.0, 9.0),
        ("GLV-HD-SZ10", "Chemical Resistant Gloves Size 10", 50, "PAIR", 270.0, 9.0, 9.0),
        ("HLM-VGUARD-WHT", "Safety Helmet White", 60, "PCS", 310.0, 9.0, 9.0),
    ]
    for row in supp_b_data:
        ws_b.append(list(row))

    ws_b.append([])
    ws_b.append(["Freight Charge", "", "", "", 12500.0, 9.0, 9.0])
    ws_b.append(["Packing Charge", "", "", "", 3500.0, 9.0, 9.0])

    path_b = target_dir / "supplier_b_bharat.xlsx"
    wb_b.save(path_b)
    paths["SUPP-B"] = path_b

    # ---------------------------------------------------------------------
    # SUPPLIER C: UOM Variations (FT for cable), Discounts, Unified IGST
    # ---------------------------------------------------------------------
    wb_c = openpyxl.Workbook()
    ws_c = wb_c.active
    ws_c.title = "Quotation_Continental"
    ws_c.append(["Continental Prime Sourcing Ltd", "", "", "", "", "", ""])
    ws_c.append(["Offer: CPS-2026-V1", "", "", "", "", "", ""])
    ws_c.append(["Payment: 100% Advance | Delivery: 21 Days", "", "", "", "", "", ""])
    ws_c.append([])
    ws_c.append(["Part Number", "Description", "Quoted Qty", "UOM", "Unit Price", "Disc %", "IGST %"])

    # Notice: Cables quoted in FT (1 MTR = 3.28084 FT)
    # Cable 6sq: 1000 MTR = 3280.84 FT @ 70.0 INR/FT (equiv to ~229.66 INR/MTR)
    # Cable 10sq: 500 MTR = 1640.42 FT @ 110.0 INR/FT (equiv to ~360.89 INR/MTR)
    supp_c_data = [
        ("SKF-6205-2RS", "Ball Bearing 25x52x15mm", 100, "PCS", 125.0, 5.0, 18.0),
        ("SKF-6206-2RS", "Ball Bearing 30x62x16mm", 50, "PCS", 170.0, 5.0, 18.0),
        ("POL-6SQ-CU", "Copper Armoured Flexible Cable 6 Sq.mm", 3280.84, "FT", 70.0, 0.0, 18.0),
        ("POL-10SQ-CU", "Copper Armoured Flexible Cable 10 Sq.mm", 1640.42, "FT", 110.0, 0.0, 18.0),
        ("SS-M8-40", "Hex Socket Cap Screw M8x40mm", 2000, "PCS", 9.00, 10.0, 18.0),
        ("SS-M8-50", "Hex Socket Cap Screw M8x50mm", 1500, "PCS", 11.00, 10.0, 18.0),
        ("UNB-M12-60", "Hex Bolt M12x60mm Grade 8.8", 800, "PCS", 19.00, 5.0, 18.0),
        ("UNB-NUT-M12", "Hex Nut M12 Grade 8.8", 1600, "PCS", 7.00, 5.0, 18.0),
        ("LNT-BV-2IN", "Cast Steel Ball Valve 2-inch 150#", 20, "PCS", 4900.0, 5.0, 18.0),
        ("LNT-CV-1IN", "Stainless Steel Check Valve 1-inch", 30, "PCS", 2250.0, 5.0, 18.0),
        ("SCH-LC1D32", "AC Power Contactor 32A 230V", 40, "PCS", 1700.0, 5.0, 18.0),
        ("SCH-NSX100", "MCCB 100A 3P 36kA", 15, "PCS", 6500.0, 5.0, 18.0),
        ("YOK-PT-10B", "Pressure Transmitter 0-10 Bar", 10, "PCS", 19000.0, 5.0, 18.0),
        ("RAD-PT100-100", "RTD Sensor Pt100", 25, "PCS", 1300.0, 5.0, 18.0),
        ("SIE-5.5KW-4P", "Motor 5.5kW 415V 4-Pole", 5, "PCS", 33000.0, 5.0, 18.0),
        ("ABB-ACS580-5.5", "ABB VFD 5.5kW 400V", 5, "PCS", 43000.0, 5.0, 18.0),
        ("KLN-SWG-2IN", "Spiral Wound Gasket 2-inch 150#", 100, "PCS", 190.0, 10.0, 18.0),
        ("PARK-100R2-0.5", "Hydraulic Hose 1/2-inch 100R2", 200, "MTR", 430.0, 5.0, 18.0),
        ("ANS-GLV-10", "Safety Nitrile Gloves Size 10", 50, "PAIR", 290.0, 5.0, 18.0),
        ("MSA-HLM-WHT", "Safety Helmet White Ratchet", 60, "PCS", 330.0, 5.0, 18.0),
    ]
    for row in supp_c_data:
        ws_c.append(list(row))

    path_c = target_dir / "supplier_c_continental.xlsx"
    wb_c.save(path_c)
    paths["SUPP-C"] = path_c

    # ---------------------------------------------------------------------
    # SUPPLIER D: USD Currency (85.00 INR), Volume Pricing Tiers
    # ---------------------------------------------------------------------
    wb_d = openpyxl.Workbook()
    ws_d = wb_d.active
    ws_d.title = "Global_USD_Quote"
    ws_d.append(["Delta Global Trading LLC", "", "", "", "", ""])
    ws_d.append(["Quote Ref: DGT-USD-2026-55", "", "", "", "", ""])
    ws_d.append(["Currency: USD | Terms: Net 45 Days | Incoterms: CIF Mumbai", "", "", "", "", ""])
    ws_d.append([])
    ws_d.append(["Part Number", "Description", "Quantity", "UOM", "Unit Price (USD)", "Tax %"])

    # USD base prices (at 85.0 INR/USD rate)
    supp_d_data = [
        ("SKF-6205-2RS", "Ball Bearing 25x52x15mm", 100, "PCS", 1.35, 0.0),    # 1.35 * 85 = 114.75 INR
        ("SKF-6206-2RS", "Ball Bearing 30x62x16mm", 50, "PCS", 1.85, 0.0),     # 1.85 * 85 = 157.25 INR
        ("POL-6SQ-CU", "Copper Armoured Cable 6 Sq.mm", 1000, "MTR", 2.70, 0.0),# 2.70 * 85 = 229.50 INR
        ("POL-10SQ-CU", "Copper Armoured Cable 10 Sq.mm", 500, "MTR", 4.30, 0.0),# 4.30 * 85 = 365.50 INR
        ("SS-M8-40", "Hex Socket Screw M8x40mm SS304", 2000, "PCS", 0.09, 0.0), # 0.09 * 85 = 7.65 INR
        ("SS-M8-50", "Hex Socket Screw M8x50mm SS304", 1500, "PCS", 0.11, 0.0), # 0.11 * 85 = 9.35 INR
        ("UNB-M12-60", "Hex Bolt M12x60mm Grade 8.8", 800, "PCS", 0.19, 0.0),  # 0.19 * 85 = 16.15 INR
        ("UNB-NUT-M12", "Hex Nut M12 Grade 8.8", 1600, "PCS", 0.07, 0.0),     # 0.07 * 85 = 5.95 INR
        ("LNT-BV-2IN", "Cast Steel Ball Valve 2-inch 150#", 20, "PCS", 54.0, 0.0), # 54 * 85 = 4590.0 INR
        ("LNT-CV-1IN", "Check Valve 1-inch NPT", 30, "PCS", 24.5, 0.0),        # 24.5 * 85 = 2082.5 INR
        ("SCH-LC1D32", "AC Power Contactor 32A 230V", 40, "PCS", 18.5, 0.0),   # 18.5 * 85 = 1572.5 INR
        ("SCH-NSX100", "MCCB 100A 3P 36kA", 15, "PCS", 72.0, 0.0),            # 72 * 85 = 6120.0 INR
        ("YOK-PT-10B", "Pressure Transmitter 0-10 Bar", 10, "PCS", 210.0, 0.0),# 210 * 85 = 17850.0 INR
        ("RAD-PT100-100", "RTD Sensor Pt100 100mm", 25, "PCS", 14.0, 0.0),     # 14 * 85 = 1190.0 INR
        ("SIE-5.5KW-4P", "Motor 5.5kW 415V 4-Pole", 5, "PCS", 360.0, 0.0),     # 360 * 85 = 30600.0 INR
        ("ABB-ACS580-5.5", "VFD 5.5kW 400V Drive", 5, "PCS", 475.0, 0.0),      # 475 * 85 = 40375.0 INR
        ("KLN-SWG-2IN", "Spiral Wound Gasket 2-inch", 100, "PCS", 2.00, 0.0),   # 2.0 * 85 = 170.0 INR
        ("PARK-100R2-0.5", "Hydraulic Hose 1/2-inch 100R2", 200, "MTR", 4.70, 0.0), # 4.7 * 85 = 399.50 INR
        ("ANS-GLV-10", "Nitrile Gloves Size 10", 50, "PAIR", 3.10, 0.0),       # 3.1 * 85 = 263.50 INR
        ("MSA-HLM-WHT", "Safety Helmet White Ratchet", 60, "PCS", 3.50, 0.0),   # 3.5 * 85 = 297.50 INR
    ]
    for row in supp_d_data:
        ws_d.append(list(row))

    path_d = target_dir / "supplier_d_delta.xlsx"
    wb_d.save(path_d)
    paths["SUPP-D"] = path_d

    # ---------------------------------------------------------------------
    # SUPPLIER E: Incomplete, Review-Required items, Invalid UOM, Unmatched item
    # ---------------------------------------------------------------------
    wb_e = openpyxl.Workbook()
    ws_e = wb_e.active
    ws_e.title = "Surplus_Offer"
    ws_e.append(["Elite Surplus Traders", "", "", "", "", ""])
    ws_e.append(["Quote: EST-2026-REV", "", "", "", "", ""])
    ws_e.append([])
    ws_e.append(["Part Number", "Description", "Quantity", "UOM", "Unit Price", "Tax %"])

    supp_e_data = [
        ("SKF-6205-2RS", "Ball Bearing 25x52x15mm", 100, "PCS", 110.0, 18.0),
        ("SKF-6206-2RS", "Ball Bearing 30x62x16mm", 50, "PCS", 150.0, 18.0),
        ("POL-6SQ-CU", "Copper Armoured Cable 6 Sq.mm", 1000, "MTR", 230.0, 18.0),
        ("POL-10SQ-CU", "Copper Armoured Cable 10 Sq.mm", 500, "MTR", 370.0, 18.0),
        # Under-specified M8 screws -> Triggers REVIEW_REQUIRED between M8x40 and M8x50
        ("", "Hex Socket Head Cap Screw M8 Stainless Steel 304", 2000, "PCS", 7.00, 18.0),
        ("", "Hex Socket Head Cap Screw M8 Stainless Steel 304", 1500, "PCS", 7.50, 18.0),
        ("UNB-M12-60", "Hex Bolt M12x60mm Grade 8.8", 800, "PCS", 16.00, 18.0),
        ("UNB-NUT-M12", "Hex Nut M12 Grade 8.8", 1600, "PCS", 5.50, 18.0),
        ("LNT-BV-2IN", "Ball Valve 2-inch 150#", 20, "PCS", 4500.0, 18.0),
        ("LNT-CV-1IN", "Check Valve 1-inch NPT", 30, "PCS", 2000.0, 18.0),
        ("SCH-LC1D32", "AC Power Contactor 32A 230V", 40, "PCS", 1550.0, 18.0),
        ("SCH-NSX100", "MCCB 100A 3P 36kA", 15, "PCS", 6000.0, 18.0),
        ("YOK-PT-10B", "Pressure Transmitter 0-10 Bar", 10, "PCS", 17500.0, 18.0),
        ("RAD-PT100-100", "RTD Sensor Pt100", 25, "PCS", 1150.0, 18.0),
        ("SIE-5.5KW-4P", "Motor 5.5kW 415V 4-Pole", 5, "PCS", 30000.0, 18.0),
        ("ABB-ACS580-5.5", "VFD 5.5kW 400V", 5, "PCS", 40000.0, 18.0),
        ("KLN-SWG-2IN", "Gasket 2-inch 150#", 100, "PCS", 160.0, 18.0),
        # Invalid UOM: Hydraulic Hose quoted in KG instead of MTR
        ("PARK-100R2-0.5", "Hydraulic Hose 1/2-inch 100R2", 200, "KG", 400.0, 18.0),
        # Unmatched surplus item:
        ("OBS-ACT-50", "Obsolete Pneumatic Actuator 50mm", 10, "PCS", 999.0, 18.0),
        ("MSA-HLM-WHT", "Safety Helmet White", 60, "PCS", 290.0, 18.0),
    ]
    for row in supp_e_data:
        ws_e.append(list(row))

    path_e = target_dir / "supplier_e_elite.xlsx"
    wb_e.save(path_e)
    paths["SUPP-E"] = path_e

    return paths
