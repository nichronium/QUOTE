"""
Comprehensive 24-Scenario Universality Test Suite for:
Universal Procurement Intelligence Foundation + Enterprise Human-in-the-Loop Matching Workbench
"""

from datetime import datetime
from decimal import Decimal
import json
import os
import shutil
import tempfile
import uuid
import pytest

from core.canonical_quote import CanonicalQuote, ExtractionMetadata, Provenance, QuoteItem
from matching.models import (
    DecisionBand,
    EvidenceItem,
    ItemMasterRecord,
    ItemStatus,
    MatchCandidate,
    MatchedQuoteItem,
    MatchMethod,
    MatchStatus,
    RFQLineItem,
    SupplierMappingRecord,
)
from matching.matcher import ItemMatcher
from matching.uom_resolver import UOMResolver
from app import services


def test_scenario_01_mechanical_item_matching():
    """Scenario 1: Mechanical item (Bearing, M8 Hex Bolt)."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="M-001",
            internal_sku="BEAR-6205-2RS",
            canonical_description="Deep Groove Ball Bearing 6205-2RS Rubber Sealed",
            stocking_uom="PCS",
            brand="SKF",
            specifications={"size": "25x52x15mm", "type": "ball bearing"}
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="SKF Deep Groove Ball Bearing 6205-2RS",
        quoted_qty=Decimal("10.0"),
        quoted_uom="PCS",
        unit_price=Decimal("450.0"),
        provenance=Provenance(source_file="quote1.xlsx", sheet_name="Sheet1", row_index=2)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "BEAR-6205-2RS"
    assert res.decision_band in [DecisionBand.HIGH_CONFIDENCE, DecisionBand.BULK_CANDIDATE]
    assert not res.has_hard_conflict


def test_scenario_02_electrical_item_matching():
    """Scenario 2: Electrical item (Copper Armoured Cable)."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="E-001",
            internal_sku="CABL-4C-6SQ",
            canonical_description="4 Core 6 sq mm Copper Armoured XLPE Cable",
            stocking_uom="MTR",
            brand="Polycab"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="Polycab 4-Core 6sq.mm Copper Flexible Armoured Cable",
        quoted_qty=Decimal("500.0"),
        quoted_uom="MTR",
        unit_price=Decimal("280.0"),
        provenance=Provenance(source_file="quote2.pdf", page_number=1, row_index=5)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "CABL-4C-6SQ"
    assert not res.has_hard_conflict


def test_scenario_03_electronics_semiconductor_matching():
    """Scenario 3: Electronics / IC / MCU."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="EL-001",
            internal_sku="MCU-STM32F407",
            manufacturer_part_number="STM32F407VGT6",
            canonical_description="ARM Cortex-M4 32-bit Microcontroller LQFP-100",
            stocking_uom="PCS",
            brand="STMicroelectronics"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        supplier_part_number="STM32F407VGT6",
        raw_description="STMicro STM32F407VGT6 MCU 168MHz",
        quoted_qty=Decimal("1000.0"),
        quoted_uom="PCS",
        unit_price=Decimal("8.50"),
        provenance=Provenance(source_file="mouser.csv", row_index=1)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.match_status == MatchStatus.EXACT_MATCH
    assert res.decision_band == DecisionBand.HIGH_CONFIDENCE
    assert res.item_master_match.candidate_sku == "MCU-STM32F407"


def test_scenario_04_raw_material_chemical_matching():
    """Scenario 4: Raw Material / Chemicals (SS304 Sheet, Solvents)."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="RM-001",
            internal_sku="MAT-SS304-2MM",
            canonical_description="Stainless Steel Sheet Grade 304 2mm 4x8ft",
            stocking_uom="KG",
            brand="Jindal"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="Jindal SS304 Sheet 2.0mm thickness",
        quoted_qty=Decimal("1500.0"),
        quoted_uom="KG",
        unit_price=Decimal("220.0"),
        provenance=Provenance(source_file="metal.xlsx", sheet_name="Pricing", row_index=4)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "MAT-SS304-2MM"
    assert not res.has_hard_conflict


def test_scenario_05_packaging_item_matching():
    """Scenario 5: Packaging item (Corrugated Boxes)."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="PKG-001",
            internal_sku="BOX-3PLY-12X10X8",
            canonical_description="3 Ply Corrugated Master Box 12x10x8 inch",
            stocking_uom="PCS"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="3-Ply Corrugated Shipping Box 12x10x8 inch",
        quoted_qty=Decimal("5000.0"),
        quoted_uom="PCS",
        unit_price=Decimal("15.0"),
        provenance=Provenance(source_file="pkg.csv", row_index=2)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "BOX-3PLY-12X10X8"


def test_scenario_06_office_supplies_matching():
    """Scenario 6: Office / Administrative supplies (A4 Paper)."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="OFF-001",
            internal_sku="PAP-A4-75GSM",
            canonical_description="JK Copier A4 Paper 75 GSM 500 Sheets Ream",
            stocking_uom="BOX"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="JK Copier Paper A4 75gsm",
        quoted_qty=Decimal("50.0"),
        quoted_uom="BOX",
        unit_price=Decimal("1200.0"),
        provenance=Provenance(source_file="stationery.pdf", page_number=1, row_index=1)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "PAP-A4-75GSM"


def test_scenario_07_it_hardware_matching():
    """Scenario 7: IT Hardware (Dell Latitude Laptop, Server RAM)."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="IT-001",
            internal_sku="LAP-DELL-5430",
            canonical_description="Dell Latitude 5430 Laptop i5 12th Gen 16GB 512GB SSD",
            stocking_uom="PCS",
            brand="Dell"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="Dell Latitude 5430 Notebook Intel i5 16GB 512GB SSD 14 inch",
        quoted_qty=Decimal("25.0"),
        quoted_uom="PCS",
        unit_price=Decimal("65000.0"),
        provenance=Provenance(source_file="it_quote.xlsx", sheet_name="Hardware", row_index=3)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "LAP-DELL-5430"
    assert not res.has_hard_conflict


def test_scenario_08_software_license_matching():
    """Scenario 8: Software / License / Cloud Subscription."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="SW-001",
            internal_sku="LIC-ADOBE-CC-ANNUAL",
            canonical_description="Adobe Creative Cloud All Apps Annual Enterprise License",
            stocking_uom="USER",
            brand="Adobe"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="Adobe CC All Apps Enterprise Subscription 1-Year",
        quoted_qty=Decimal("10.0"),
        quoted_uom="USER",
        unit_price=Decimal("48000.0"),
        provenance=Provenance(source_file="adobe.pdf", page_number=1, row_index=1)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "LIC-ADOBE-CC-ANNUAL"
    assert not res.has_hard_conflict


def test_scenario_09_service_and_maintenance_matching():
    """Scenario 9: Service / Maintenance Contract."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="SRV-001",
            internal_sku="SVC-HVAC-AMC-ANNUAL",
            canonical_description="Comprehensive Annual Maintenance Contract for HVAC Chillers",
            stocking_uom="SERVICE"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="HVAC Chiller Comprehensive AMC 1 Year Support",
        quoted_qty=Decimal("1.0"),
        quoted_uom="SERVICE",
        unit_price=Decimal("250000.0"),
        provenance=Provenance(source_file="hvac.docx", row_index=1)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "SVC-HVAC-AMC-ANNUAL"


def test_scenario_10_consumables_matching():
    """Scenario 10: Consumable / Generic PPE item."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="PPE-001",
            internal_sku="PPE-GLV-NITRILE-L",
            canonical_description="Nitrile Examination Gloves Powder Free Size L Pack of 100",
            stocking_uom="BOX"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="Nitrile Powder Free Gloves Large 100/box",
        quoted_qty=Decimal("200.0"),
        quoted_uom="BOX",
        unit_price=Decimal("350.0"),
        provenance=Provenance(source_file="ppe.csv", row_index=2)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "PPE-GLV-NITRILE-L"


def test_scenario_11_custom_engineered_part():
    """Scenario 11: Custom Engineered Part with drawings."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="CUST-001",
            internal_sku="DWG-FLG-300-DN150",
            canonical_description="Custom Forged Blind Flange Class 300 DN150 per DWG-2026-08",
            stocking_uom="PCS"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="Forged Flange Cl 300 DN150 As Per DWG-2026-08",
        quoted_qty=Decimal("12.0"),
        quoted_uom="PCS",
        unit_price=Decimal("8200.0"),
        provenance=Provenance(source_file="flange_quote.xlsx", row_index=2)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "DWG-FLG-300-DN150"


def test_scenario_12_missing_quantity_and_uom_never_fabricated():
    """Scenario 12: Missing quantity / UOM must NOT fabricate defaults (no 100, 1, or PCS)."""
    matcher = ItemMatcher()
    resolver = UOMResolver()
    master = [
        ItemMasterRecord(
            internal_item_id="MISC-001",
            internal_sku="DOC-NON-PROC",
            canonical_description="Student Registration Verification",
            stocking_uom="N/A"
        )
    ]
    # Quote item with None quantity and None UOM (e.g. non-procurement or unstated)
    quote_item = QuoteItem(
        line_index=0,
        raw_description="Student List Verification",
        quoted_qty=None,
        quoted_uom=None,
        unit_price=None,
        provenance=Provenance(source_file="students.csv", row_index=1)
    )
    assert quote_item.quoted_qty is None
    assert quote_item.quoted_uom is None

    uom_res = resolver.resolve_uom_conversion(source_uom=None, target_uom=None, quoted_quantity=None)
    assert uom_res.is_compatible is True
    assert uom_res.converted_quantity is None
    assert uom_res.conversion_factor == Decimal("1.0")

    res = matcher.match_quote_item(quote_item, master)
    assert res.quote_item.quoted_qty is None
    assert res.quote_item.quoted_uom is None


def test_scenario_13_arbitrary_generic_uoms():
    """Scenario 13: Universal support for arbitrary UOMs."""
    resolver = UOMResolver()
    # Test Count
    res_count = resolver.resolve_uom_conversion("NOS", "PCS", Decimal("50.0"))
    assert res_count.is_compatible is True
    assert res_count.converted_quantity == Decimal("50.0")

    # Test Digital License
    res_lic = resolver.resolve_uom_conversion("USER", "SEAT", Decimal("20.0"))
    assert res_lic.is_compatible is True
    assert res_lic.converted_quantity == Decimal("20.0")

    # Test Time
    res_time = resolver.resolve_uom_conversion("HOUR", "HRS", Decimal("100.0"))
    assert res_time.is_compatible is True
    assert res_time.converted_quantity == Decimal("100.0")


def test_scenario_14_dimension_safety_and_rejection():
    """Scenario 14: Cross-dimension without approved factor is safely rejected."""
    resolver = UOMResolver()
    res = resolver.resolve_uom_conversion("KG", "PCS", Decimal("100.0"))
    assert res.is_compatible is False
    assert res.conversion_method == "INCOMPATIBLE"


def test_scenario_15_exact_sku_matching():
    """Scenario 15: Exact Internal SKU matching."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="EX-001",
            internal_sku="SKU-9900-ABC",
            canonical_description="Precision Optical Sensor 24VDC",
            stocking_uom="PCS"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        supplier_part_number="SKU-9900-ABC",
        raw_description="Optical Sensor",
        quoted_qty=Decimal("5.0"),
        quoted_uom="PCS",
        unit_price=Decimal("1200.0"),
        provenance=Provenance(source_file="q.csv", row_index=1)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.match_status == MatchStatus.EXACT_MATCH
    assert res.decision_band == DecisionBand.HIGH_CONFIDENCE
    assert res.item_master_match.match_method == MatchMethod.INTERNAL_SKU_EXACT


def test_scenario_16_approved_supplier_part_number_matching():
    """Scenario 16: Approved supplier part number matching."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="SUPP-001",
            internal_sku="CAT-100",
            approved_supplier_part_numbers=["VEND-PART-XYZ"],
            canonical_description="Hydraulic Cylinder 50mm Stroke",
            stocking_uom="PCS"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        supplier_part_number="VEND-PART-XYZ",
        raw_description="Hydraulic Cylinder",
        quoted_qty=Decimal("2.0"),
        quoted_uom="PCS",
        unit_price=Decimal("15000.0"),
        provenance=Provenance(source_file="q.csv", row_index=1)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.match_status == MatchStatus.EXACT_MATCH
    assert res.item_master_match.candidate_sku == "CAT-100"
    assert res.item_master_match.match_method == MatchMethod.SUPPLIER_SKU_EXACT


def test_scenario_17_manufacturer_part_number_matching():
    """Scenario 17: Manufacturer Part Number matching."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="MPN-001",
            internal_sku="VALV-SOL-24V",
            manufacturer_part_number="EV-24V-08",
            canonical_description="Pneumatic Solenoid Valve 24VDC 1/4 inch",
            stocking_uom="PCS"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        supplier_part_number="EV-24V-08",
        raw_description="Solenoid Valve 24V",
        quoted_qty=Decimal("10.0"),
        quoted_uom="PCS",
        unit_price=Decimal("2200.0"),
        provenance=Provenance(source_file="q.csv", row_index=1)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.match_status == MatchStatus.EXACT_MATCH
    assert res.item_master_match.candidate_sku == "VALV-SOL-24V"
    assert res.item_master_match.match_method == MatchMethod.MANUFACTURER_PN_EXACT


def test_scenario_18_fuzzy_description_matching():
    """Scenario 18: Multi-signal fuzzy description matching."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="FUZZ-001",
            internal_sku="TOOL-HEX-WRENCH-SET",
            canonical_description="9 Piece Metric Hex Key Wrench Set 1.5mm to 10mm",
            stocking_uom="SET",
            brand="Stanley"
        )
    ]
    quote_item = QuoteItem(
        line_index=0,
        raw_description="Stanley 9pc Metric Hex Allen Key Wrench Set 1.5-10mm",
        quoted_qty=Decimal("15.0"),
        quoted_uom="SET",
        unit_price=Decimal("650.0"),
        provenance=Provenance(source_file="tools.xlsx", row_index=3)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.item_master_match is not None
    assert res.item_master_match.candidate_sku == "TOOL-HEX-WRENCH-SET"
    assert res.item_master_match.match_score >= 0.75


def test_scenario_19_hard_conflict_blocks_high_score():
    """Scenario 19: Hard conflict (e.g. 6sqmm vs 10sqmm) strictly blocks auto-match."""
    matcher = ItemMatcher()
    master = [
        ItemMasterRecord(
            internal_item_id="CABL-001",
            internal_sku="CABL-10SQ",
            canonical_description="Polycab 4 Core 10 sq mm Copper Flexible Cable",
            stocking_uom="MTR",
            brand="Polycab"
        )
    ]
    # Quote is for 6 sq mm cable (contradictory specification)
    quote_item = QuoteItem(
        line_index=0,
        raw_description="Polycab 4 Core 6 sq mm Copper Flexible Cable",
        quoted_qty=Decimal("100.0"),
        quoted_uom="MTR",
        unit_price=Decimal("150.0"),
        provenance=Provenance(source_file="q.csv", row_index=1)
    )
    res = matcher.match_quote_item(quote_item, master)
    assert res.has_hard_conflict is True
    assert res.decision_band == DecisionBand.BLOCKED_CONFLICT
    assert res.match_status == MatchStatus.REVIEW_REQUIRED
    assert any("conflict" in c.lower() or "mismatch" in c.lower() for c in res.conflict_reasons)


def test_scenario_20_bulk_confirmation_flow(tmp_path, monkeypatch):
    """Scenario 20: 80-94% Strong candidate bulk human confirmation."""
    data_dir = tmp_path / "test_runs"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "DATA_DIR", data_dir)
    im_dir = data_dir / "item_master"
    im_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "ITEM_MASTER_DIR", im_dir)
    monkeypatch.setattr(services, "SUPPLIER_MAPPINGS_FILE", data_dir / "supplier_mappings.json")

    qid = "test-quote-bulk"
    q_dir = data_dir / qid
    (q_dir / "extracted").mkdir(parents=True)

    items = [
        QuoteItem(line_index=0, raw_description="6205-2RS Bearing", quoted_qty=Decimal("10"), quoted_uom="PCS", unit_price=Decimal("100"), provenance=Provenance(source_file="q.csv", row_index=1)),
        QuoteItem(line_index=1, raw_description="6206-2RS Bearing", quoted_qty=Decimal("10"), quoted_uom="PCS", unit_price=Decimal("120"), provenance=Provenance(source_file="q.csv", row_index=2))
    ]
    canonical = CanonicalQuote(
        quote_id=qid,
        supplier_raw_name="Bearing Supplier",
        currency="INR",
        items=items,
        extraction_metadata=ExtractionMetadata(source_file_name="q.csv", source_file_hash="abc", parser_used="csv")
    )
    with open(q_dir / "extracted" / "canonical_quote.json", "w", encoding="utf-8") as f:
        f.write(canonical.model_dump_json(indent=2))

    master = [
        ItemMasterRecord(internal_item_id="B1", internal_sku="BEAR-6205", canonical_description="Ball Bearing 6205-2RS", stocking_uom="PCS"),
        ItemMasterRecord(internal_item_id="B2", internal_sku="BEAR-6206", canonical_description="Ball Bearing 6206-2RS", stocking_uom="PCS"),
    ]
    with open(im_dir / "catalog.json", "w", encoding="utf-8") as f:
        json.dump([m.model_dump(mode="json") for m in master], f)

    matched = services.run_matching_for_quote(qid)
    assert len(matched) == 2

    # Bulk confirm lines [0, 1]
    res = services.bulk_confirm_matches(qid, [0, 1])
    assert res["status"] == "success"
    assert res["confirmed_count"] == 2

    reloaded = services.get_matched_items(qid)
    assert reloaded[0].match_status == MatchStatus.EXACT_MATCH
    assert reloaded[1].match_status == MatchStatus.EXACT_MATCH


def test_scenario_21_guided_manual_review_resolution(tmp_path, monkeypatch):
    """Scenario 21: Guided manual individual review (50-79% ambiguous lines)."""
    data_dir = tmp_path / "test_runs"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "DATA_DIR", data_dir)
    im_dir = data_dir / "item_master"
    im_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "ITEM_MASTER_DIR", im_dir)
    monkeypatch.setattr(services, "SUPPLIER_MAPPINGS_FILE", data_dir / "supplier_mappings.json")

    qid = "test-quote-manual"
    q_dir = data_dir / qid
    (q_dir / "extracted").mkdir(parents=True)

    items = [
        QuoteItem(line_index=0, raw_description="Generic Stainless Fastener", quoted_qty=Decimal("100"), quoted_uom="PCS", unit_price=Decimal("5"), provenance=Provenance(source_file="q.csv", row_index=1))
    ]
    canonical = CanonicalQuote(
        quote_id=qid,
        supplier_raw_name="Fastener Vendor",
        currency="INR",
        items=items,
        extraction_metadata=ExtractionMetadata(source_file_name="q.csv", source_file_hash="abc", parser_used="csv")
    )
    with open(q_dir / "extracted" / "canonical_quote.json", "w", encoding="utf-8") as f:
        f.write(canonical.model_dump_json(indent=2))

    master = [
        ItemMasterRecord(internal_item_id="F1", internal_sku="SCREW-M6-SS304", canonical_description="Stainless Steel M6x20mm Screw", stocking_uom="PCS"),
        ItemMasterRecord(internal_item_id="F2", internal_sku="BOLT-M8-SS304", canonical_description="Stainless Steel M8x40mm Bolt", stocking_uom="PCS"),
    ]
    with open(im_dir / "catalog.json", "w", encoding="utf-8") as f:
        json.dump([m.model_dump(mode="json") for m in master], f)

    matched = services.run_matching_for_quote(qid)
    # Reviewer manually overrides candidate to SCREW-M6-SS304
    services.resolve_match_candidate(qid, line_index=0, chosen_candidate_sku="SCREW-M6-SS304", action="ACCEPT")

    reloaded = services.get_matched_items(qid)
    assert reloaded[0].match_status == MatchStatus.EXACT_MATCH
    assert reloaded[0].item_master_match.candidate_sku == "SCREW-M6-SS304"


def test_scenario_22_valid_unmatched_state(tmp_path, monkeypatch):
    """Scenario 22: Non-catalog items remain legitimately UNMATCHED without forcing fake matches."""
    data_dir = tmp_path / "test_runs"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "DATA_DIR", data_dir)
    im_dir = data_dir / "item_master"
    im_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "ITEM_MASTER_DIR", im_dir)
    monkeypatch.setattr(services, "SUPPLIER_MAPPINGS_FILE", data_dir / "supplier_mappings.json")

    qid = "test-quote-unmatched"
    q_dir = data_dir / qid
    (q_dir / "extracted").mkdir(parents=True)

    items = [
        QuoteItem(line_index=0, raw_description="Bespoke Titanium Space Component", quoted_qty=Decimal("1"), quoted_uom="PCS", unit_price=Decimal("500000"), provenance=Provenance(source_file="q.csv", row_index=1))
    ]
    canonical = CanonicalQuote(
        quote_id=qid,
        supplier_raw_name="Aero Supplier",
        currency="INR",
        items=items,
        extraction_metadata=ExtractionMetadata(source_file_name="q.csv", source_file_hash="abc", parser_used="csv")
    )
    with open(q_dir / "extracted" / "canonical_quote.json", "w", encoding="utf-8") as f:
        f.write(canonical.model_dump_json(indent=2))

    master = [
        ItemMasterRecord(internal_item_id="F1", internal_sku="OFFICE-CHAIR", canonical_description="Ergonomic Mesh Office Chair", stocking_uom="PCS")
    ]
    with open(im_dir / "catalog.json", "w", encoding="utf-8") as f:
        json.dump([m.model_dump(mode="json") for m in master], f)

    matched = services.run_matching_for_quote(qid)
    assert matched[0].match_status == MatchStatus.UNMATCHED
    assert matched[0].decision_band == DecisionBand.LOW_CONFIDENCE


def test_scenario_23_grouped_identical_mappings(tmp_path, monkeypatch):
    """Scenario 23: Group identical proposed mappings (1 decision resolves N identical lines)."""
    data_dir = tmp_path / "test_runs"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "DATA_DIR", data_dir)
    im_dir = data_dir / "item_master"
    im_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "ITEM_MASTER_DIR", im_dir)
    monkeypatch.setattr(services, "SUPPLIER_MAPPINGS_FILE", data_dir / "supplier_mappings.json")

    qid = "test-quote-grouped"
    q_dir = data_dir / qid
    (q_dir / "extracted").mkdir(parents=True)

    # 4 lines quoting the exact same item
    items = [
        QuoteItem(line_index=0, raw_description="6205 Ball Bearing", quoted_qty=Decimal("10"), quoted_uom="PCS", unit_price=Decimal("100"), provenance=Provenance(source_file="q.csv", row_index=1)),
        QuoteItem(line_index=1, raw_description="6205 Ball Bearing", quoted_qty=Decimal("20"), quoted_uom="PCS", unit_price=Decimal("100"), provenance=Provenance(source_file="q.csv", row_index=2)),
        QuoteItem(line_index=2, raw_description="6205 Ball Bearing", quoted_qty=Decimal("30"), quoted_uom="PCS", unit_price=Decimal("100"), provenance=Provenance(source_file="q.csv", row_index=3)),
        QuoteItem(line_index=3, raw_description="6205 Ball Bearing", quoted_qty=Decimal("40"), quoted_uom="PCS", unit_price=Decimal("100"), provenance=Provenance(source_file="q.csv", row_index=4)),
    ]
    canonical = CanonicalQuote(
        quote_id=qid,
        supplier_raw_name="Bearing Supplier",
        currency="INR",
        items=items,
        extraction_metadata=ExtractionMetadata(source_file_name="q.csv", source_file_hash="abc", parser_used="csv")
    )
    with open(q_dir / "extracted" / "canonical_quote.json", "w", encoding="utf-8") as f:
        f.write(canonical.model_dump_json(indent=2))

    master = [
        ItemMasterRecord(internal_item_id="B1", internal_sku="BEAR-6205-2RS", canonical_description="Ball Bearing 6205-2RS Rubber Sealed", stocking_uom="PCS")
    ]
    with open(im_dir / "catalog.json", "w", encoding="utf-8") as f:
        json.dump([m.model_dump(mode="json") for m in master], f)

    matched = services.run_matching_for_quote(qid)
    workbench = services.get_quote_matching_workbench_data(qid)
    assert len(workbench["grouped_candidates"]) >= 1
    grp = workbench["grouped_candidates"][0]
    assert grp["count"] == 4

    # Resolve group with 1 action
    res = services.resolve_grouped_matches(qid, grp["group_key"], "BEAR-6205-2RS")
    assert res["status"] == "success"
    assert res["resolved_count"] == 4

    reloaded = services.get_matched_items(qid)
    for m in reloaded:
        assert m.match_status == MatchStatus.EXACT_MATCH
        assert m.item_master_match.candidate_sku == "BEAR-6205-2RS"


def test_scenario_24_supplier_memory_reuse_across_quotes(tmp_path, monkeypatch):
    """Scenario 24: Confirmed Supplier X + Supplier PN -> Internal SKU is remembered and auto-recalled."""
    data_dir = tmp_path / "test_runs"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "DATA_DIR", data_dir)
    im_dir = data_dir / "item_master"
    im_dir.mkdir(parents=True)
    monkeypatch.setattr(services, "ITEM_MASTER_DIR", im_dir)
    monkeypatch.setattr(services, "SUPPLIER_MAPPINGS_FILE", data_dir / "supplier_mappings.json")

    master = [
        ItemMasterRecord(internal_item_id="ITEM-999", internal_sku="SKU-PUMP-500", canonical_description="Industrial Water Pump 500W", stocking_uom="PCS")
    ]
    with open(im_dir / "catalog.json", "w", encoding="utf-8") as f:
        json.dump([m.model_dump(mode="json") for m in master], f)

    # 1. First Quote from "Acme Industrial"
    qid1 = "quote-round-1"
    q_dir1 = data_dir / qid1
    (q_dir1 / "extracted").mkdir(parents=True)
    items1 = [
        QuoteItem(line_index=0, supplier_part_number="ACME-PMP-50", raw_description="Pump 500W", quoted_qty=Decimal("2"), quoted_uom="PCS", unit_price=Decimal("15000"), provenance=Provenance(source_file="q1.csv", row_index=1))
    ]
    canonical1 = CanonicalQuote(
        quote_id=qid1,
        supplier_raw_name="Acme Industrial",
        currency="INR",
        items=items1,
        extraction_metadata=ExtractionMetadata(source_file_name="q1.csv", source_file_hash="abc", parser_used="csv")
    )
    with open(q_dir1 / "extracted" / "canonical_quote.json", "w", encoding="utf-8") as f:
        f.write(canonical1.model_dump_json(indent=2))

    services.run_matching_for_quote(qid1)
    # Reviewer confirms mapping
    services.resolve_match_candidate(qid1, line_index=0, chosen_candidate_sku="SKU-PUMP-500", action="ACCEPT")

    # Verify mapping was saved in memory
    mappings = services.get_supplier_mappings()
    assert any(m.supplier_part_number == "ACME-PMP-50" and m.internal_sku == "SKU-PUMP-500" for m in mappings)

    # 2. Second Quote in future RFQ from "Acme Industrial" with same part number
    qid2 = "quote-round-2"
    q_dir2 = data_dir / qid2
    (q_dir2 / "extracted").mkdir(parents=True)
    items2 = [
        QuoteItem(line_index=0, supplier_part_number="ACME-PMP-50", raw_description="Industrial Water Pump", quoted_qty=Decimal("5"), quoted_uom="PCS", unit_price=Decimal("14500"), provenance=Provenance(source_file="q2.csv", row_index=1))
    ]
    canonical2 = CanonicalQuote(
        quote_id=qid2,
        supplier_raw_name="Acme Industrial",
        currency="INR",
        items=items2,
        extraction_metadata=ExtractionMetadata(source_file_name="q2.csv", source_file_hash="abc", parser_used="csv")
    )
    with open(q_dir2 / "extracted" / "canonical_quote.json", "w", encoding="utf-8") as f:
        f.write(canonical2.model_dump_json(indent=2))

    matched2 = services.run_matching_for_quote(qid2)
    assert matched2[0].match_status == MatchStatus.EXACT_MATCH
    assert matched2[0].decision_band == DecisionBand.HIGH_CONFIDENCE
    assert matched2[0].item_master_match.candidate_sku == "SKU-PUMP-500"
    assert matched2[0].item_master_match.match_method == MatchMethod.HISTORICAL_SUPPLIER_MAPPING
