"""
Held-Out Supplier Quotation Corpus with Ground Truth Annotations.

Contains 16 realistic, anonymized supplier quotations covering diverse
industries (machining, electrical, fasteners, hydraulics, raw materials, electronics),
formats (XLSX, CSV, multi-sheet, multiline, tiered, flat relational), and
expected reconciliation statuses.
"""

from decimal import Decimal
import io
import openpyxl
from typing import Any, Callable, Dict, List, Tuple


def _to_xlsx_bytes(wb_builder: Callable[[openpyxl.Workbook], None]) -> bytes:
    wb = openpyxl.Workbook()
    wb_builder(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ============================================================================
# H01: Precision Fasteners (XLSX, Standard Tabular + Discount + GST)
# ============================================================================
def get_H01_precision_fasteners() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Quotation"
        ws.append(["Apex Precision Fasteners Pvt Ltd"])
        ws.append(["Quotation Ref: APF/2026/0912", "Date: 2026-08-15"])
        ws.append([])
        ws.append(["Sr", "Part No", "Description", "Qty", "UOM", "Unit Rate", "Disc %", "GST %", "Total"])
        ws.append([1, "SS-M8-40", "Hex Socket Cap Screw M8x40 SS316", 500, "NOS", 18.50, 5, 18, 9368.50])
        ws.append([2, "SS-M8-NUT", "Nyloc Nut M8 SS316 DIN 985", 500, "NOS", 4.20, 0, 18, 2478.00])
        ws.append([3, "SS-M8-WSH", "Spring Washer M8 DIN 127B", 1000, "NOS", 1.10, 0, 18, 1298.00])
        ws.append([])
        ws.append(["Grand Total", "", "", "", "", "", "", "", 13144.50])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H01_precision_fasteners.xlsx",
        "supplier_name": "Apex Precision Fasteners Pvt Ltd",
        "quote_number": "APF/2026/0912",
        "currency": "INR",
        "item_count": 3,
        "items": [
            {"desc": "Hex Socket Cap Screw M8x40 SS316", "qty": Decimal("500"), "uom": "PCS", "price": Decimal("18.50"), "disc": Decimal("5"), "tax": Decimal("18"), "landed": Decimal("10369.38")}, # 500 * 18.50 * 0.95 * 1.18 = 10369.375 -> 10369.38
            {"desc": "Nyloc Nut M8 SS316 DIN 985", "qty": Decimal("500"), "uom": "PCS", "price": Decimal("4.20"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("2478.00")},
            {"desc": "Spring Washer M8 DIN 127B", "qty": Decimal("1000"), "uom": "PCS", "price": Decimal("1.10"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("1298.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": Decimal("13144.50"),
        "expected_reconciliation": "DISCREPANCY"  # 10369.38 + 2478 + 1298 = 14145.38 != stated 13144.50
    }
    return "H01_precision_fasteners.xlsx", data, truth



# ============================================================================
# H02: Electrical Switchgear (Multi-Sheet XLSX, Quote on Sheet 2)
# ============================================================================
def get_H02_electrical_switchgear() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws1 = wb.active
        ws1.title = "Company_Profile"
        ws1.append(["ElectroPower Systems Ltd — Leading Switchgear Manufacturer"])
        ws1.append(["ISO 9001:2015 Certified"])

        ws2 = wb.create_sheet(title="Commercial_Offer")
        ws2.append(["ElectroPower Systems Ltd"])
        ws2.append(["Quote Ref: EPS/Q/2026/44", "Date: 2026-08-18"])
        ws2.append([])
        ws2.append(["Item Code", "Description", "Quantity", "UOM", "Price (INR)", "GST"])
        ws2.append(["MCB-4P-63A", "4 Pole MCB 63A 10kA C-Curve", 20, "PCS", 1250.00, 18])
        ws2.append(["RCCB-4P-40A", "4 Pole RCCB 40A 30mA Type AC", 10, "PCS", 2400.00, 18])
        ws2.append(["SPD-4P-40KA", "Surge Protection Device Type 2 40kA", 5, "PCS", 3800.00, 18])
        ws2.append([])
        ws2.append(["Grand Total", "", "", "", 80240.00])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H02_electrical_switchgear.xlsx",
        "supplier_name": "ElectroPower Systems Ltd",
        "quote_number": "EPS/Q/2026/44",
        "currency": "INR",
        "item_count": 3,
        "items": [
            {"desc": "4 Pole MCB 63A 10kA C-Curve", "qty": Decimal("20"), "uom": "PCS", "price": Decimal("1250.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("29500.00")},
            {"desc": "4 Pole RCCB 40A 30mA Type AC", "qty": Decimal("10"), "uom": "PCS", "price": Decimal("2400.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("28320.00")},
            {"desc": "Surge Protection Device Type 2 40kA", "qty": Decimal("5"), "uom": "PCS", "price": Decimal("3800.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("22420.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": Decimal("80240.00"),
        "expected_reconciliation": "RECONCILED"  # 29500 + 28320 + 22420 = 80240.00
    }
    return "H02_electrical_switchgear.xlsx", data, truth


# ============================================================================
# H03: Hydraulic Valves (CSV, Semicolon-Delimited)
# ============================================================================
def get_H03_hydraulic_valves_csv() -> Tuple[str, bytes, Dict[str, Any]]:
    csv_text = """Supplier: HydroTech Fluid Power Solutions
Quote: HT-2026-778;Date: 2026-08-19

Part Number;Item Description;Qty;UOM;Unit Price;Tax Rate
HV-4WE6-E;Directional Control Valve 4WE6E 24VDC;4;PCS;4200.00;18%
PRV-DBDS10;Direct Operated Relief Valve DBDS10;2;PCS;3100.00;18%
FCV-2FRM6;Flow Control Valve 2FRM6B;3;PCS;5600.00;18%
"""
    data = csv_text.encode("utf-8")
    truth = {
        "file_name": "H03_hydraulic_valves.csv",
        "supplier_name": "HydroTech Fluid Power Solutions",
        "quote_number": "HT-2026-778",
        "currency": "INR",
        "item_count": 3,
        "items": [
            {"desc": "Directional Control Valve 4WE6E 24VDC", "qty": Decimal("4"), "uom": "PCS", "price": Decimal("4200.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("19824.00")},
            {"desc": "Direct Operated Relief Valve DBDS10", "qty": Decimal("2"), "uom": "PCS", "price": Decimal("3100.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("7316.00")},
            {"desc": "Flow Control Valve 2FRM6B", "qty": Decimal("3"), "uom": "PCS", "price": Decimal("5600.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("19824.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": None,
        "expected_reconciliation": "CALCULATED_ONLY"
    }
    return "H03_hydraulic_valves.csv", data, truth


# ============================================================================
# H04: Steel Pipes (Multiline Descriptions + HSN Codes)
# ============================================================================
def get_H04_steel_pipes_multiline() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Quotation"
        ws.append(["Tubes & Pipes India Private Limited"])
        ws.append(["Quote Ref: TPI-2026-990", "Date: 2026-08-11"])
        ws.append([])
        ws.append(["Item", "Description", "HSN", "Qty", "UOM", "Rate (INR)", "GST %"])
        ws.append([1, "Seamless Carbon Steel Pipe ASTM A106 Gr.B\nSize: 2 inch NB Sch 40\nLength: 6 Meter Random", "7304", 120, "MTR", 620.00, 18])
        ws.append([2, "Seamless Carbon Steel Pipe ASTM A106 Gr.B\nSize: 4 inch NB Sch 40\nLength: 6 Meter Random", "7304", 60, "MTR", 1450.00, 18])
        ws.append([])
        ws.append(["Grand Total", "", "", "", "", "", 190452.00])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H04_steel_pipes_multiline.xlsx",
        "supplier_name": "Tubes & Pipes India Private Limited",
        "quote_number": "TPI-2026-990",
        "currency": "INR",
        "item_count": 2,
        "items": [
            {"desc": "Seamless Carbon Steel Pipe ASTM A106 Gr.B", "qty": Decimal("120"), "uom": "MTR", "price": Decimal("620.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("87792.00")},
            {"desc": "Seamless Carbon Steel Pipe ASTM A106 Gr.B", "qty": Decimal("60"), "uom": "MTR", "price": Decimal("1450.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("102660.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": Decimal("190452.00"),
        "expected_reconciliation": "RECONCILED"  # 87792 + 102660 = 190452.00
    }
    return "H04_steel_pipes_multiline.xlsx", data, truth


# ============================================================================
# H05: Copper Cables with Volume Tier Pricing
# ============================================================================
def get_H05_copper_cables_tier_pricing() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Price_Offer"
        ws.append(["National Cables & Wire Corp"])
        ws.append(["Quotation Ref: NCW-2026-88"])
        ws.append([])
        ws.append(["Part No", "Description", "Qty", "UOM", "Quoted Rate", "GST %"])
        ws.append(["CAB-6SQ", "Copper Armoured Flexible Cable 6 Sq.mm", 1000, "MTR", 140.00, 18])
        ws.append([])
        ws.append(["VOLUME PRICING TIERS — CAB-6SQ"])
        ws.append(["Min Qty", "Max Qty", "Unit Price"])
        ws.append([1, 499, 140.00])
        ws.append([500, 1999, 132.00])
        ws.append([2000, "", 125.00])
        ws.append([])
        ws.append(["Grand Total", "", "", "", 165200.00])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H05_copper_cables_tier_pricing.xlsx",
        "supplier_name": "National Cables & Wire Corp",
        "quote_number": "NCW-2026-88",
        "currency": "INR",
        "item_count": 1,
        "price_tiers_count": 3,
        "items": [
            {"desc": "Copper Armoured Flexible Cable 6 Sq.mm", "qty": Decimal("1000"), "uom": "MTR", "price": Decimal("140.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("165200.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": Decimal("165200.00"),
        "expected_reconciliation": "RECONCILED"
    }
    return "H05_copper_cables_tier_pricing.xlsx", data, truth


# ============================================================================
# H06: Raw Materials with Surcharges (Freight + Packing + Insurance)
# ============================================================================
def get_H06_raw_materials_charges() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Quote"
        ws.append(["Metals & Minerals Logistics Ltd"])
        ws.append(["Ref: MML/2026/301", "Date: 2026-08-17"])
        ws.append([])
        ws.append(["Description", "Qty", "UOM", "Rate", "GST %"])
        ws.append(["Aluminium Ingot 99.7% Pure", 2000, "KG", 220.00, 18])
        ws.append([])
        ws.append(["Freight Charges:", 8500.00, "18%"])
        ws.append(["Wooden Packing:", 2500.00, "0%"])
        ws.append(["Transit Insurance:", 1500.00, "18%"])
        ws.append([])
        ws.append(["Grand Total", "", "", 533500.00])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H06_raw_materials_charges.xlsx",
        "supplier_name": "Metals & Minerals Logistics Ltd",
        "quote_number": "MML/2026/301",
        "currency": "INR",
        "item_count": 1,
        "items": [
            {"desc": "Aluminium Ingot 99.7% Pure", "qty": Decimal("2000"), "uom": "KG", "price": Decimal("220.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("519200.00")},
        ],
        "charges_count": 3,
        "stated_grand_total": Decimal("533500.00"),
        "expected_reconciliation": "RECONCILED"  # Items: 519200 + Freight: 10030 + Packing: 2500 + Insurance: 1770 = 533500
    }
    return "H06_raw_materials_charges.xlsx", data, truth



# ============================================================================
# H07: CNC Tooling Flat Relational Table (Repeated Metadata Column)
# ============================================================================
def get_H07_cnc_tooling_flat_relational() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Pricing"
        ws.append(["Supplier", "Quote No", "Part Number", "Description", "Qty", "UOM", "Unit Price", "Tax %"])
        ws.append(["Kyocera Cutting Tools India", "KYO-2026-55", "CNMG120408-GM", "Carbide Turning Insert CNMG", 100, "PCS", 280.00, 18])
        ws.append(["Kyocera Cutting Tools India", "KYO-2026-55", "WNMG080408-GM", "Carbide Turning Insert WNMG", 100, "PCS", 310.00, 18])
        ws.append(["Kyocera Cutting Tools India", "KYO-2026-55", "TNMG160408-GM", "Carbide Turning Insert TNMG", 50, "PCS", 260.00, 18])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H07_cnc_tooling_flat_relational.xlsx",
        "supplier_name": "Kyocera Cutting Tools India",
        "quote_number": "KYO-2026-55",
        "currency": "INR",
        "item_count": 3,
        "items": [
            {"desc": "Carbide Turning Insert CNMG", "qty": Decimal("100"), "uom": "PCS", "price": Decimal("280.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("33040.00")},
            {"desc": "Carbide Turning Insert WNMG", "qty": Decimal("100"), "uom": "PCS", "price": Decimal("310.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("36580.00")},
            {"desc": "Carbide Turning Insert TNMG", "qty": Decimal("50"), "uom": "PCS", "price": Decimal("260.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("15340.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": None,
        "expected_reconciliation": "CALCULATED_ONLY"
    }
    return "H07_cnc_tooling_flat_relational.xlsx", data, truth


# ============================================================================
# H08: Safety Equipment (Arithmetic Discrepancy)
# ============================================================================
def get_H08_safety_equipment_discrepancy() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Safety_Quote"
        ws.append(["Karam Safety Equipment Pvt Ltd"])
        ws.append(["Quote Ref: KRM-2026-11"])
        ws.append([])
        ws.append(["Description", "Qty", "UOM", "Rate", "Tax %"])
        ws.append(["Full Body Safety Harness PN56", 50, "PCS", 1450.00, 18])  # 50 * 1450 * 1.18 = 85550.00
        ws.append(["Safety Helmet with Chin Strap", 100, "PCS", 220.00, 18])   # 100 * 220 * 1.18 = 25960.00
        ws.append([])
        # Real calculated total = 111,510.00; Stated fabricated = 150,000.00
        ws.append(["Grand Total", "", "", 150000.00])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H08_safety_equipment_discrepancy.xlsx",
        "supplier_name": "Karam Safety Equipment Pvt Ltd",
        "quote_number": "KRM-2026-11",
        "currency": "INR",
        "item_count": 2,
        "items": [
            {"desc": "Full Body Safety Harness PN56", "qty": Decimal("50"), "uom": "PCS", "price": Decimal("1450.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("85550.00")},
            {"desc": "Safety Helmet with Chin Strap", "qty": Decimal("100"), "uom": "PCS", "price": Decimal("220.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("25960.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": Decimal("150000.00"),
        "expected_reconciliation": "DISCREPANCY"
    }
    return "H08_safety_equipment_discrepancy.xlsx", data, truth


# ============================================================================
# H09: Pneumatic Cylinders (No Stated Grand Total -> CALCULATED_ONLY)
# ============================================================================
def get_H09_pneumatic_cylinders() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Offer"
        ws.append(["Festo Pneumatics India Pvt Ltd"])
        ws.append(["Quote Ref: FST-2026-441", "Date: 2026-08-14"])
        ws.append([])
        ws.append(["Part No", "Description", "Qty", "UOM", "Price", "GST %"])
        ws.append(["DSNU-25-50", "Round Cylinder DSNU-25-50-PPV-A", 15, "PCS", 2100.00, 18])
        ws.append(["DSBC-32-100", "Standard Cylinder DSBC-32-100-PPVA", 8, "PCS", 4800.00, 18])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H09_pneumatic_cylinders.xlsx",
        "supplier_name": "Festo Pneumatics India Pvt Ltd",
        "quote_number": "FST-2026-441",
        "currency": "INR",
        "item_count": 2,
        "items": [
            {"desc": "Round Cylinder DSNU-25-50-PPV-A", "qty": Decimal("15"), "uom": "PCS", "price": Decimal("2100.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("37170.00")},
            {"desc": "Standard Cylinder DSBC-32-100-PPVA", "qty": Decimal("8"), "uom": "PCS", "price": Decimal("4800.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("45312.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": None,
        "expected_reconciliation": "CALCULATED_ONLY"
    }
    return "H09_pneumatic_cylinders.xlsx", data, truth


# ============================================================================
# H10: Conveyor Belts (Pipe-Delimited CSV)
# ============================================================================
def get_H10_conveyor_belts_csv() -> Tuple[str, bytes, Dict[str, Any]]:
    csv_text = """Supplier: Fenner Conveyor Systems Ltd
Quote Ref: FCS-2026-809
Currency: INR

Item|Description|Qty|UOM|Unit Price|Discount %|GST %
1|Rough Top Conveyor Belt 3 Ply 600mm|50|MTR|1850.00|10|18
2|Chevron Cleated Conveyor Belt 4 Ply 800mm|30|MTR|3400.00|5|18
"""
    data = csv_text.encode("utf-8")
    truth = {
        "file_name": "H10_conveyor_belts.csv",
        "supplier_name": "Fenner Conveyor Systems Ltd",
        "quote_number": "FCS-2026-809",
        "currency": "INR",
        "item_count": 2,
        "items": [
            {"desc": "Rough Top Conveyor Belt 3 Ply 600mm", "qty": Decimal("50"), "uom": "MTR", "price": Decimal("1850.00"), "disc": Decimal("10"), "tax": Decimal("18"), "landed": Decimal("98235.00")}, # 50 * 1850 * 0.90 * 1.18 = 98235.00
            {"desc": "Chevron Cleated Conveyor Belt 4 Ply 800mm", "qty": Decimal("30"), "uom": "MTR", "price": Decimal("3400.00"), "disc": Decimal("5"), "tax": Decimal("18"), "landed": Decimal("114342.00")}, # 30 * 3400 * 0.95 * 1.18 = 114342.00
        ],
        "charges_count": 0,
        "stated_grand_total": None,
        "expected_reconciliation": "CALCULATED_ONLY"
    }
    return "H10_conveyor_belts.csv", data, truth


# ============================================================================
# H11: Solar Inverters (Multi-Tax Rates: 5%, 12%, 18%)
# ============================================================================
def get_H11_solar_inverters_multi_tax() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Solar_Offer"
        ws.append(["SunGrow Power Systems Pvt Ltd"])
        ws.append(["Ref No: SG-2026-339", "Date: 2026-08-16"])
        ws.append([])
        ws.append(["Description", "Qty", "UOM", "Rate", "Tax Rate"])
        ws.append(["Solar PV Modules Monocrystalline 540W", 100, "NOS", 12500.00, 12])  # 100 * 12500 * 1.12 = 1400000.00
        ws.append(["Solar On-Grid Inverter 50kW 3-Phase", 2, "NOS", 185000.00, 18])      # 2 * 185000 * 1.18 = 436600.00
        ws.append(["Solar DC Cable 4 Sq.mm Crosslink", 500, "MTR", 45.00, 18])           # 500 * 45 * 1.18 = 26550.00
        ws.append([])
        ws.append(["Grand Total", "", "", 1863150.00])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H11_solar_inverters.xlsx",
        "supplier_name": "SunGrow Power Systems Pvt Ltd",
        "quote_number": "SG-2026-339",
        "currency": "INR",
        "item_count": 3,
        "items": [
            {"desc": "Solar PV Modules Monocrystalline 540W", "qty": Decimal("100"), "uom": "PCS", "price": Decimal("12500.00"), "disc": Decimal("0"), "tax": Decimal("12"), "landed": Decimal("1400000.00")},
            {"desc": "Solar On-Grid Inverter 50kW 3-Phase", "qty": Decimal("2"), "uom": "PCS", "price": Decimal("185000.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("436600.00")},
            {"desc": "Solar DC Cable 4 Sq.mm Crosslink", "qty": Decimal("500"), "uom": "MTR", "price": Decimal("45.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("26550.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": Decimal("1863150.00"),
        "expected_reconciliation": "RECONCILED"  # 1400000 + 436600 + 26550 = 1863150.00
    }
    return "H11_solar_inverters.xlsx", data, truth



# ============================================================================
# H12: Bearing Housings (Hidden Valid Rows)
# ============================================================================
def get_H12_bearing_housings_hidden_items() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Quote"
        ws.append(["SKF Bearing Distributors Ltd"])
        ws.append(["Quote Ref: SKF/2026/78"])
        ws.append([])
        ws.append(["Item No", "Description", "Qty", "UOM", "Rate", "GST %"])
        ws.append([1, "Plummer Block Housing SNL 511-609", 10, "PCS", 3200.00, 18])
        ws.append([2, "Plummer Block Housing SNL 512-610", 10, "PCS", 3800.00, 18])
        ws.append([3, "Internal Cost Sheet Row", 0, "", 0.0, 0])
        ws.row_dimensions[6].hidden = True  # Row 6 is helper row
        ws.append([4, "Felt Seal Set TSN 511 G", 20, "PCS", 450.00, 18])
        ws.row_dimensions[7].hidden = True  # Row 7 is valid hidden product

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H12_bearing_housings.xlsx",
        "supplier_name": "SKF Bearing Distributors Ltd",
        "quote_number": "SKF/2026/78",
        "currency": "INR",
        "item_count": 3,
        "items": [
            {"desc": "Plummer Block Housing SNL 511-609", "qty": Decimal("10"), "uom": "PCS", "price": Decimal("3200.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("37760.00")},
            {"desc": "Plummer Block Housing SNL 512-610", "qty": Decimal("10"), "uom": "PCS", "price": Decimal("3800.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("44840.00")},
            {"desc": "Felt Seal Set TSN 511 G", "qty": Decimal("20"), "uom": "PCS", "price": Decimal("450.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("10620.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": None,
        "expected_reconciliation": "CALCULATED_ONLY"
    }
    return "H12_bearing_housings.xlsx", data, truth


# ============================================================================
# H13: Automation Terms Only (Zero Line Items -> INSUFFICIENT_DATA)
# ============================================================================
def get_H13_automation_terms_only() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Terms"
        ws.append(["Siemens Industry Software India"])
        ws.append(["Annual Maintenance Contract Terms & Conditions"])
        ws.append(["Validity: 30 Days"])
        ws.append(["Payment: 100% Advance"])
        ws.append(["Scope: Software Updates & Remote Support"])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H13_automation_terms.xlsx",
        "supplier_name": "Siemens Industry Software India",
        "quote_number": None,
        "currency": "INR",
        "item_count": 0,
        "items": [],
        "charges_count": 0,
        "stated_grand_total": None,
        "expected_reconciliation": "INSUFFICIENT_DATA"
    }
    return "H13_automation_terms.xlsx", data, truth


# ============================================================================
# H14: Cutting Tools (Unusual Headers: Designation, Pcs, Base Rate)
# ============================================================================
def get_H14_cutting_tools_unusual_headers() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Tools"
        ws.append(["Sandvik Coromant India Pvt Ltd"])
        ws.append(["Ref: SVK-2026-919", "Dated: 2026-08-10"])
        ws.append([])
        ws.append(["Designation", "Pcs", "Base Rate", "GST %"])
        ws.append(["CoroDrill 860-GM 8.5mm Solid Carbide", 5, 4800.00, 18])
        ws.append(["CoroMill 390 Milling Cutter Shank 25mm", 2, 12500.00, 18])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H14_cutting_tools.xlsx",
        "supplier_name": "Sandvik Coromant India Pvt Ltd",
        "quote_number": "SVK-2026-919",
        "currency": "INR",
        "item_count": 2,
        "items": [
            {"desc": "CoroDrill 860-GM 8.5mm Solid Carbide", "qty": Decimal("5"), "uom": "PCS", "price": Decimal("4800.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("28320.00")},
            {"desc": "CoroMill 390 Milling Cutter Shank 25mm", "qty": Decimal("2"), "uom": "PCS", "price": Decimal("12500.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("29500.00")},
        ],
        "charges_count": 0,
        "stated_grand_total": None,
        "expected_reconciliation": "CALCULATED_ONLY"
    }
    return "H14_cutting_tools.xlsx", data, truth


# ============================================================================
# H15: Welding Supplies (Delivery & Payment Terms Extraction)
# ============================================================================
def get_H15_welding_supplies() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Quotation"
        ws.append(["ESAB India Limited"])
        ws.append(["Quote No: ESAB/2026/812", "Date: 2026-08-12"])
        ws.append(["Payment Terms: 30 Days Credit"])
        ws.append(["Delivery Terms: Ex-Works Mumbai, Lead Time: 14 Days"])
        ws.append([])
        ws.append(["Description", "Qty", "UOM", "Rate", "Tax %"])
        ws.append(["MIG Welding Wire ER70S-6 1.2mm Spool 15kg", 20, "NOS", 1950.00, 18])
        ws.append(["TIG Welding Rod ER308L 2.4mm Box 5kg", 10, "NOS", 2800.00, 18])
        ws.append([])
        ws.append(["Grand Total", "", "", 79060.00])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H15_welding_supplies.xlsx",
        "supplier_name": "ESAB India Limited",
        "quote_number": "ESAB/2026/812",
        "currency": "INR",
        "item_count": 2,
        "items": [
            {"desc": "MIG Welding Wire ER70S-6 1.2mm Spool 15kg", "qty": Decimal("20"), "uom": "PCS", "price": Decimal("1950.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("46020.00")}, # 20 * 1950 * 1.18 = 46020
            {"desc": "TIG Welding Rod ER308L 2.4mm Box 5kg", "qty": Decimal("10"), "uom": "PCS", "price": Decimal("2800.00"), "disc": Decimal("0"), "tax": Decimal("18"), "landed": Decimal("33040.00")}, # 10 * 2800 * 1.18 = 33040
        ],
        "charges_count": 0,
        "stated_grand_total": Decimal("79060.00"),
        "expected_reconciliation": "RECONCILED"  # 46020 + 33040 = 79060.00
    }
    return "H15_welding_supplies.xlsx", data, truth



# ============================================================================
# H16: Electronic Components (Item Discounts + Freight + Tax)
# ============================================================================
def get_H16_electronic_components() -> Tuple[str, bytes, Dict[str, Any]]:
    def build(wb):
        ws = wb.active
        ws.title = "Quote"
        ws.append(["Mouser Electronic Solutions Pvt Ltd"])
        ws.append(["Quote Ref: MOU-2026-661", "Date: 2026-08-13"])
        ws.append([])
        ws.append(["Part No", "Description", "Qty", "UOM", "Unit Price", "Discount %", "GST %"])
        ws.append(["STM32F407VGT6", "MCU 32-Bit ARM Cortex-M4 168MHz", 100, "PCS", 450.00, 10, 18])  # 100 * 450 * 0.9 * 1.18 = 47790.00
        ws.append(["ESP32-WROOM-32D", "Wi-Fi + BLE Module 4MB Flash", 200, "PCS", 220.00, 5, 18])      # 200 * 220 * 0.95 * 1.18 = 49324.00
        ws.append([])
        ws.append(["Freight & Shipping:", 800.00, "18%"])  # 800 * 1.18 = 944.00
        ws.append([])
        ws.append(["Grand Total", "", "", "", "", "", 98058.00])

    data = _to_xlsx_bytes(build)
    truth = {
        "file_name": "H16_electronic_components.xlsx",
        "supplier_name": "Mouser Electronic Solutions Pvt Ltd",
        "quote_number": "MOU-2026-661",
        "currency": "INR",
        "item_count": 2,
        "items": [
            {"desc": "MCU 32-Bit ARM Cortex-M4 168MHz", "qty": Decimal("100"), "uom": "PCS", "price": Decimal("450.00"), "disc": Decimal("10"), "tax": Decimal("18"), "landed": Decimal("47790.00")},
            {"desc": "Wi-Fi + BLE Module 4MB Flash", "qty": Decimal("200"), "uom": "PCS", "price": Decimal("220.00"), "disc": Decimal("5"), "tax": Decimal("18"), "landed": Decimal("49324.00")},
        ],
        "charges_count": 1,
        "stated_grand_total": Decimal("98058.00"),
        "expected_reconciliation": "RECONCILED"  # 47790 + 49324 + 944 = 98058.00
    }
    return "H16_electronic_components.xlsx", data, truth


ALL_HOLDOUT_GENERATORS = [
    get_H01_precision_fasteners,
    get_H02_electrical_switchgear,
    get_H03_hydraulic_valves_csv,
    get_H04_steel_pipes_multiline,
    get_H05_copper_cables_tier_pricing,
    get_H06_raw_materials_charges,
    get_H07_cnc_tooling_flat_relational,
    get_H08_safety_equipment_discrepancy,
    get_H09_pneumatic_cylinders,
    get_H10_conveyor_belts_csv,
    get_H11_solar_inverters_multi_tax,
    get_H12_bearing_housings_hidden_items,
    get_H13_automation_terms_only,
    get_H14_cutting_tools_unusual_headers,
    get_H15_welding_supplies,
    get_H16_electronic_components,
]
