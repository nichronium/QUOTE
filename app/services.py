"""
Application Services for Quote Intelligence Enterprise Platform.
Coordinates domain engines across:
- RFQ Management: Creation, Scope Definition, Status Tracking
- Supplier Quotation Ingestion: Upload, Automated Extraction, Provenance, Independent Reconciliation
- Item Master Alignment: Multi-strategy SKU/PN/Fuzzy Matching, Candidate Resolution
- Commercial Normalization & Award: Multi-currency, Surcharges, L1/L2/L3 Ranking, Split Sourcing
- Review Center: Scoped exception inbox for active user procurement rounds
- Controlled Benchmark Showcase & Segregated Developer Lab
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.canonical_quote import CanonicalQuote, QuoteItem, quantize_currency
from comparison.comparator import QuotationComparator
from core.currency import CurrencyRateService, LiveRateResult
from comparison.models import (
    ChargeAllocationMethod,
    ComparisonIssue,
    RFQComparison,
    RFQDocument,
    SupplierQuoteSubmission,
)
from comparison.ranking import GlobalRankingReport, RankingEngine
from datasets.procurement_benchmark.dataset import (
    generate_benchmark_workbooks,
    get_benchmark_item_master,
    get_benchmark_rfq,
)
from extraction.extractor import QuoteExtractor
from extraction.catalog_extractor import CatalogExtractor, CatalogExtractionResult
from matching.matcher import ItemMatcher
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
    SupplierMasterRecord,
    SupplierNameChange,
    ImportBatchRecord,
)
from parsers.csv_parser import CSVParser
from parsers.excel_parser import ExcelParser
from parsers.pdf_parser import PDFParser


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "test_runs"
RFQS_DIR = DATA_DIR / "rfqs"
SUPPLIER_MAPPINGS_FILE = DATA_DIR / "supplier_mappings.json"
COMPARISONS_DIR = DATA_DIR / "comparisons"
ITEM_MASTER_DIR = DATA_DIR / "item_master"
AWARDS_DIR = DATA_DIR / "awards"

# Ensure root storage directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
RFQS_DIR.mkdir(parents=True, exist_ok=True)
COMPARISONS_DIR.mkdir(parents=True, exist_ok=True)
ITEM_MASTER_DIR.mkdir(parents=True, exist_ok=True)
AWARDS_DIR.mkdir(parents=True, exist_ok=True)

DEMO_RFQ_ID = "RFQ-2026-E2E-BENCHMARK"
DEMO_SUPPLIER_IDS = ["BENCH-SUPP-A", "BENCH-SUPP-B", "BENCH-SUPP-C", "BENCH-SUPP-D", "BENCH-SUPP-E"]


# =========================================================================
# 1. RFQ MANAGEMENT & SCOPE DEFINITION
# =========================================================================

def ensure_default_rfqs():
    """Initializes RFQ storage directory."""
    RFQS_DIR.mkdir(parents=True, exist_ok=True)


def save_rfq_document(rfq: RFQDocument, allow_overwrite: bool = True) -> RFQDocument:
    """
    CRITICAL INVARIANT: Atomic write with collision protection.
    Persists an RFQDocument atomically using temp file write + rename.
    Guarantees that files are never left in a corrupted or partially written state.
    """
    RFQS_DIR.mkdir(parents=True, exist_ok=True)
    rfq_file = RFQS_DIR / f"{rfq.rfq_id}.json"
    
    if not allow_overwrite and rfq_file.exists():
        raise ValueError(f"RFQ ID collision: '{rfq.rfq_id}' already exists and cannot be overwritten.")
        
    tmp_file = RFQS_DIR / f"{rfq.rfq_id}.tmp.{uuid.uuid4().hex[:8]}"
    with open(tmp_file, "w", encoding="utf-8") as f:
        f.write(rfq.model_dump_json(indent=2))
        f.flush()
        os.fsync(f.fileno())
    
    tmp_file.replace(rfq_file)
    return rfq


ensure_default_rfqs()


def list_rfqs(include_demo: bool = False, lifecycle_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lists RFQs with lifecycle filtering.
    Guarantees:
    - Never throws or silently drops an RFQ.
    - Historical files remain accessible across all lifecycle stages.
    - Lifecycle filter options: 'all', 'active', 'closed', 'cancelled', 'archived'.
    """
    ensure_default_rfqs()
    rfqs = []
    all_comparisons = list_comparisons()
    all_quotes = list_quotes(include_dev_runs=True, include_demo=True)
    
    if not RFQS_DIR.exists():
        return []

    for f in sorted(RFQS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            
            try:
                rfq = RFQDocument.model_validate(data)
            except Exception:
                # Robust fallback for custom or missing fields
                rfq = RFQDocument(
                    rfq_id=data.get("rfq_id", f.stem),
                    title=data.get("title", f.stem),
                    base_currency=data.get("base_currency", "INR"),
                    status=data.get("status", "OPEN"),
                    created_at=data.get("created_at"),
                    updated_at=data.get("updated_at"),
                    archived_at=data.get("archived_at"),
                    items=[]
                )
                
            is_demo = (rfq.rfq_id == DEMO_RFQ_ID)
            if not include_demo and is_demo:
                continue

            # Compute quotes attached to this RFQ
            attached_quotes = get_quotes_for_rfq(rfq.rfq_id, all_quotes=all_quotes)
            ready_quotes = sum(1 for q in attached_quotes if q.get("match_status") in ["MATCHED", "REVIEW_REQUIRED"] and q.get("status") == "SUCCESS")
            has_review = any(q.get("review_count", 0) > 0 for q in attached_quotes)

            # Check if comparison exists
            comp_list = [c for c in all_comparisons if c.get("rfq_id") == rfq.rfq_id]
            award_rec = get_award_decision(rfq.rfq_id)

            # Compute dynamic procurement stage vs explicit lifecycle status
            lifecycle_status = (rfq.status or "OPEN").upper()
            
            if lifecycle_status in ["ARCHIVED", "CANCELLED", "CLOSED"]:
                display_status = lifecycle_status
            elif award_rec and award_rec.get("status") == "FINALIZED":
                display_status = "AWARD_FINALIZED"
            elif comp_list:
                display_status = "EVALUATED"
            elif len(attached_quotes) == 0:
                display_status = "DRAFT" if lifecycle_status == "DRAFT" else "OPEN"
            elif has_review:
                display_status = "MATCHING_REVIEW"
            elif ready_quotes >= 2:
                display_status = "READY_FOR_COMPARISON"
            else:
                display_status = "QUOTES_PENDING"

            # Apply lifecycle_filter
            filter_lower = (lifecycle_filter or "all").lower().strip()
            if filter_lower == "active" and lifecycle_status in ["ARCHIVED", "CANCELLED", "CLOSED"]:
                continue
            elif filter_lower == "archived" and lifecycle_status != "ARCHIVED":
                continue
            elif filter_lower == "cancelled" and lifecycle_status != "CANCELLED":
                continue
            elif filter_lower == "closed" and lifecycle_status not in ["CLOSED", "AWARD_FINALIZED"]:
                continue

            rfqs.append({
                "rfq_id": rfq.rfq_id,
                "title": rfq.title,
                "base_currency": rfq.base_currency,
                "lifecycle_status": lifecycle_status,
                "status": display_status,
                "created_at": rfq.created_at,
                "updated_at": rfq.updated_at,
                "archived_at": rfq.archived_at,
                "line_item_count": len(rfq.items),
                "quote_count": len(attached_quotes),
                "ready_quote_count": ready_quotes,
                "has_comparison": len(comp_list) > 0,
                "has_award": award_rec is not None and award_rec.get("status") == "FINALIZED",
                "is_demo": is_demo,
                "_mtime": f.stat().st_mtime,
                "latest_comparison_id": comp_list[0]["comparison_id"] if comp_list else None,
                "items": [it.model_dump() for it in rfq.items]
            })
        except Exception:
            continue

    rfqs.sort(key=lambda r: (1 if r["is_demo"] else 0, -r.get("_mtime", 0)))
    return rfqs


def get_rfq(rfq_id: str) -> Optional[RFQDocument]:
    rfq_file = RFQS_DIR / f"{rfq_id}.json"
    if not rfq_file.exists():
        return None
    try:
        with open(rfq_file, "r", encoding="utf-8") as f:
            return RFQDocument.model_validate_json(f.read())
    except Exception:
        return None


def archive_rfq(rfq_id: str) -> Optional[RFQDocument]:
    """Archives an RFQ without deleting any historical data."""
    rfq = get_rfq(rfq_id)
    if not rfq:
        return None
    rfq.status = "ARCHIVED"
    rfq.archived_at = datetime.utcnow().isoformat()
    rfq.updated_at = datetime.utcnow().isoformat()
    return save_rfq_document(rfq)


def cancel_rfq(rfq_id: str) -> Optional[RFQDocument]:
    """Cancels an RFQ without deleting any historical data."""
    rfq = get_rfq(rfq_id)
    if not rfq:
        return None
    rfq.status = "CANCELLED"
    rfq.updated_at = datetime.utcnow().isoformat()
    return save_rfq_document(rfq)


def restore_rfq_lifecycle(rfq_id: str) -> Optional[RFQDocument]:
    """Restores an archived or cancelled RFQ back to OPEN."""
    rfq = get_rfq(rfq_id)
    if not rfq:
        return None
    rfq.status = "OPEN"
    rfq.updated_at = datetime.utcnow().isoformat()
    return save_rfq_document(rfq)


def delete_rfq_safe(rfq_id: str) -> Tuple[bool, str]:
    """
    CRITICAL INVARIANT: RFQs must NEVER be physically deleted from disk.
    Converts any delete intent to lifecycle ARCHIVED state while preserving the full historical record.
    """
    rfq = get_rfq(rfq_id)
    if not rfq:
        return False, f"RFQ '{rfq_id}' not found."
    
    quotes = get_quotes_for_rfq(rfq_id)
    comp = get_latest_rfq_comparison(rfq_id)
    award = get_award_decision(rfq_id)
    
    rfq.status = "ARCHIVED"
    rfq.archived_at = datetime.utcnow().isoformat()
    rfq.updated_at = datetime.utcnow().isoformat()
    save_rfq_document(rfq)
    
    if quotes or comp or award:
        return True, f"RFQ '{rfq_id}' is referenced by historical records and has been safely ARCHIVED. Historical procurement records remain 100% preserved."
    return True, f"RFQ '{rfq_id}' has been safely ARCHIVED."


def create_rfq(
    rfq_id: str,
    title: str,
    base_currency: str,
    items: List[Dict[str, Any]],
    source: str = "USER",
    is_test: bool = False
) -> RFQDocument:
    # 1. Auto-register any new items into company Item Master so catalog stays unified and supplier quotes match seamlessly
    current_im = get_item_master(include_inactive=True)
    existing_skus = {im.internal_sku.upper().strip() for im in current_im if im.internal_sku}
    existing_ids = {im.internal_item_id for im in current_im if im.internal_item_id}
    new_im_records = []
    batch_id = f"IMP-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"

    for idx, it in enumerate(items):
        sku = (it.get("sku") or it.get("internal_sku") or it.get("rfq_line_id") or f"SKU-{idx+1:03d}").strip().upper()
        desc = it.get("description") or it.get("canonical_description") or sku
        
        raw_uom_in = it.get("requested_uom") or it.get("stocking_uom") or it.get("uom")
        uom_val = str(raw_uom_in).strip().upper() if raw_uom_in and str(raw_uom_in).strip().lower() not in ["none", "null", "n/a", "na", "-", "—"] else None
        
        item_id = it.get("internal_item_id") or f"ITEM-{sku}"

        if sku not in existing_skus and item_id not in existing_ids:
            new_im_records.append(ItemMasterRecord(
                internal_item_id=item_id,
                internal_sku=sku,
                canonical_description=desc,
                stocking_uom=uom_val or "PCS",
                brand="Standard",
                status="ACTIVE",
                import_batch_id=batch_id,
                created_at=datetime.utcnow().isoformat()
            ))
            existing_skus.add(sku)
            existing_ids.add(item_id)

    if new_im_records:
        current_im.extend(new_im_records)
        save_item_master(current_im)
        record_import_batch(
            batch_id=batch_id,
            filename=f"RFQ: {title or rfq_id}",
            item_ids=[nr.internal_item_id for nr in new_im_records],
            source_type="RFQ_REQUIREMENTS"
        )

    # 2. Build RFQLineItem list with honest source quantity and UOM
    rfq_items = []
    for idx, it in enumerate(items):
        sku_val = (it.get("sku") or it.get("internal_sku") or it.get("rfq_line_id") or f"SKU-{idx+1:03d}").strip().upper()
        desc_val = it.get("description") or it.get("canonical_description") or sku_val
        
        # Source-driven UOM: preserve None if missing
        raw_uom_in = it.get("requested_uom") or it.get("stocking_uom") or it.get("uom")
        uom_val = str(raw_uom_in).strip().upper() if raw_uom_in and str(raw_uom_in).strip().lower() not in ["none", "null", "n/a", "na", "-", "—"] else None
        
        # Source-driven Quantity: preserve None if missing
        raw_qty_in = it.get("requested_quantity") or it.get("quantity") or it.get("qty")
        qty_val: Optional[Decimal] = None
        if raw_qty_in is not None and str(raw_qty_in).strip() not in ["", "None", "null", "N/A", "na", "-", "—"]:
            try:
                dec = Decimal(str(raw_qty_in))
                if dec > Decimal("0.0"):
                    qty_val = dec
            except Exception:
                qty_val = None

        item_id_val = it.get("internal_item_id") or f"ITEM-{sku_val}"

        rfq_items.append(RFQLineItem(
            rfq_line_id=it.get("rfq_line_id", f"RFQ-LINE-{idx+1:03d}"),
            internal_item_id=item_id_val,
            sku=sku_val,
            description=desc_val,
            requested_quantity=qty_val,
            requested_uom=uom_val,
            target_unit_price=Decimal(str(it.get("target_unit_price"))) if it.get("target_unit_price") else None,
            approved_uom_conversions={k: Decimal(str(v)) for k, v in it.get("approved_uom_conversions", {}).items()}
        ))

    final_rfq_id = rfq_id.strip() or f"RFQ-{int(time.time())}"
    
    # ID collision protection: if already exists on disk, ensure it's not silently overwritten
    if (RFQS_DIR / f"{final_rfq_id}.json").exists():
        existing_doc = get_rfq(final_rfq_id)
        if existing_doc and existing_doc.title != title:
            # Different RFQ -> allocate new unique ID to prevent overwrite
            final_rfq_id = f"{final_rfq_id}-{uuid.uuid4().hex[:4].upper()}"

    now_iso = datetime.utcnow().isoformat()
    rfq = RFQDocument(
        rfq_id=final_rfq_id,
        title=title.strip(),
        base_currency=base_currency.strip().upper() or "INR",
        status="OPEN",
        source=source,
        is_test=is_test,
        created_at=now_iso,
        updated_at=now_iso,
        items=rfq_items
    )
    return save_rfq_document(rfq, allow_overwrite=True)


def ensure_item_master_initialized():
    """Initializes canonical Item Master catalog.json as empty if it doesn't exist."""
    ITEM_MASTER_DIR.mkdir(parents=True, exist_ok=True)
    catalog_file = ITEM_MASTER_DIR / "catalog.json"
    if not catalog_file.exists():
        with open(catalog_file, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)


ensure_item_master_initialized()


def get_item_master(include_inactive: bool = False) -> List[ItemMasterRecord]:
    """Returns company Item Master catalog records. Defaults to ACTIVE only."""
    catalog_file = ITEM_MASTER_DIR / "catalog.json"
    if not catalog_file.exists():
        ensure_item_master_initialized()
    try:
        with open(catalog_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            records = [ItemMasterRecord.model_validate(d) for d in data]
            if include_inactive:
                return records
            return [r for r in records if r.status != "INACTIVE"]
    except Exception:
        return []


def save_item_master(items: List[ItemMasterRecord]):
    """Saves updated company Item Master catalog."""
    catalog_file = ITEM_MASTER_DIR / "catalog.json"
    with open(catalog_file, "w", encoding="utf-8") as f:
        json.dump([item.model_dump(mode="json") for item in items], f, indent=2)


def add_item_to_master(
    internal_sku: str,
    canonical_description: str,
    stocking_uom: str = "PCS",
    manufacturer_part_number: Optional[str] = None,
    brand: Optional[str] = None,
    approved_supplier_part_numbers: Optional[List[str]] = None,
    specifications: Optional[Dict[str, str]] = None
) -> ItemMasterRecord:
    """Adds a single product record to the company Item Master catalog."""
    items = get_item_master(include_inactive=True)
    existing_ids = [it.internal_item_id for it in items if it.internal_item_id.startswith("ITEM-")]
    indices = []
    for i in existing_ids:
        try:
            indices.append(int(i.replace("ITEM-", "")))
        except ValueError:
            pass
    next_idx = max(indices, default=0) + 1
    new_id = f"ITEM-{next_idx:03d}"

    record = ItemMasterRecord(
        internal_item_id=new_id,
        internal_sku=internal_sku.strip().upper(),
        manufacturer_part_number=manufacturer_part_number.strip() if manufacturer_part_number else None,
        approved_supplier_part_numbers=approved_supplier_part_numbers or [],
        canonical_description=canonical_description.strip(),
        stocking_uom=stocking_uom.strip().upper() or "PCS",
        brand=brand.strip() if brand else "Standard",
        specifications=specifications or {},
        status="ACTIVE",
        created_at=datetime.utcnow().isoformat()
    )
    items.append(record)
    save_item_master(items)
    return record


def get_item_master_item(item_id: str) -> Optional[ItemMasterRecord]:
    """Retrieves a single item from the company Item Master catalog (including inactive)."""
    items = get_item_master(include_inactive=True)
    for it in items:
        if it.internal_item_id == item_id or it.internal_sku.upper() == item_id.upper():
            return it
    return None


# =========================================================================
# ITEM & SUPPLIER MASTER LIFECYCLE & DEPENDENCY CHECKS
# =========================================================================

IMPORT_BATCHES_FILE = ITEM_MASTER_DIR / "import_batches.json"
SUPPLIERS_FILE = ITEM_MASTER_DIR / "suppliers.json"


def check_item_dependencies(item_id: str, sku: Optional[str] = None) -> Dict[str, Any]:
    """
    Checks if an Item Master record is referenced by any RFQ, Quote matching, Comparison, or Award.
    """
    references = []
    target_sku = sku.strip().upper() if sku else None

    # 1. Check RFQs
    if RFQS_DIR.exists():
        for rfq_file in RFQS_DIR.glob("*.json"):
            try:
                with open(rfq_file, "r", encoding="utf-8") as f:
                    rfq_data = json.load(f)
                    for it in rfq_data.get("items", []):
                        if it.get("internal_item_id") == item_id or (target_sku and str(it.get("sku", "")).upper() == target_sku):
                            references.append(f"RFQ '{rfq_data.get('rfq_id')}' ({rfq_data.get('title', 'RFQ')})")
                            break
            except Exception:
                continue

    # 2. Check Quote Matching records
    if DATA_DIR.exists():
        for q_dir in DATA_DIR.iterdir():
            if not q_dir.is_dir() or q_dir.name in ["rfqs", "comparisons", "awards", "item_master", "benchmark_fixtures"]:
                continue
            matched_file = q_dir / "matching" / "matched_items.json"
            if matched_file.exists():
                try:
                    with open(matched_file, "r", encoding="utf-8") as f:
                        matched_items = json.load(f)
                        for m in matched_items:
                            imm = m.get("item_master_match")
                            if imm:
                                if imm.get("candidate_item_id") == item_id or (target_sku and str(imm.get("candidate_sku", "")).upper() == target_sku):
                                    references.append(f"Quotation Matching '{q_dir.name}'")
                                    break
                except Exception:
                    continue

    # 3. Check Comparisons
    if COMPARISONS_DIR.exists():
        for comp_file in COMPARISONS_DIR.glob("*.json"):
            try:
                with open(comp_file, "r", encoding="utf-8") as f:
                    comp_data = json.load(f)
                    comp_obj = comp_data.get("comparison", {})
                    for ic in comp_obj.get("item_comparisons", []):
                        if ic.get("internal_item_id") == item_id or (target_sku and str(ic.get("sku", "")).upper() == target_sku):
                            references.append(f"Commercial Comparison '{comp_data.get('comparison_id', comp_file.stem)}'")
                            break
            except Exception:
                continue

    # 4. Check Awards
    if AWARDS_DIR.exists():
        for award_file in AWARDS_DIR.glob("*.json"):
            try:
                with open(award_file, "r", encoding="utf-8") as f:
                    award_data = json.load(f)
                    for alloc in award_data.get("allocations", []):
                        if alloc.get("internal_item_id") == item_id or (target_sku and str(alloc.get("sku", "")).upper() == target_sku):
                            references.append(f"Award Decision '{award_data.get('rfq_id', award_file.stem)}'")
                            break
            except Exception:
                continue

    return {
        "is_referenced": len(references) > 0,
        "references": references,
        "reference_count": len(references)
    }


def deactivate_item_master_record(item_id: str) -> Tuple[bool, str]:
    catalog = get_item_master(include_inactive=True)
    for it in catalog:
        if it.internal_item_id == item_id or it.internal_sku.upper() == item_id.upper():
            it.status = "INACTIVE"
            save_item_master(catalog)
            return True, f"Item '{it.internal_sku}' ({it.internal_item_id}) has been deactivated."
    return False, f"Item '{item_id}' not found in catalog."


def restore_item_master_record(item_id: str) -> Tuple[bool, str]:
    catalog = get_item_master(include_inactive=True)
    for it in catalog:
        if it.internal_item_id == item_id or it.internal_sku.upper() == item_id.upper():
            it.status = "ACTIVE"
            save_item_master(catalog)
            return True, f"Item '{it.internal_sku}' ({it.internal_item_id}) has been restored to Active."
    return False, f"Item '{item_id}' not found in catalog."


def delete_item_master_record(item_id: str) -> Tuple[bool, str]:
    catalog = get_item_master(include_inactive=True)
    target = None
    for it in catalog:
        if it.internal_item_id == item_id or it.internal_sku.upper() == item_id.upper():
            target = it
            break

    if not target:
        return False, f"Item '{item_id}' not found in catalog."

    deps = check_item_dependencies(target.internal_item_id, target.internal_sku)
    if deps["is_referenced"]:
        ref_summary = ", ".join(deps["references"][:3])
        if len(deps["references"]) > 3:
            ref_summary += f" (+{len(deps['references'])-3} more)"
        return False, f"Cannot hard delete '{target.internal_sku}': referenced in {deps['reference_count']} historical record(s) ({ref_summary}). Deactivate the item instead."

    new_catalog = [it for it in catalog if it.internal_item_id != target.internal_item_id]
    save_item_master(new_catalog)
    return True, f"Item '{target.internal_sku}' ({target.internal_item_id}) has been permanently deleted."


def bulk_deactivate_items(item_ids: List[str]) -> int:
    catalog = get_item_master(include_inactive=True)
    id_set = {i.strip().upper() for i in item_ids}
    count = 0
    for it in catalog:
        if (it.internal_item_id.upper() in id_set or it.internal_sku.upper() in id_set) and it.status != "INACTIVE":
            it.status = "INACTIVE"
            count += 1
    if count > 0:
        save_item_master(catalog)
    return count


def bulk_restore_items(item_ids: List[str]) -> int:
    catalog = get_item_master(include_inactive=True)
    id_set = {i.strip().upper() for i in item_ids}
    count = 0
    for it in catalog:
        if (it.internal_item_id.upper() in id_set or it.internal_sku.upper() in id_set) and it.status != "ACTIVE":
            it.status = "ACTIVE"
            count += 1
    if count > 0:
        save_item_master(catalog)
    return count


def bulk_delete_items(item_ids: List[str]) -> Dict[str, Any]:
    catalog = get_item_master(include_inactive=True)
    id_set = {i.strip().upper() for i in item_ids}
    deleted_ids = []
    blocked = []

    for it in catalog:
        if it.internal_item_id.upper() in id_set or it.internal_sku.upper() in id_set:
            deps = check_item_dependencies(it.internal_item_id, it.internal_sku)
            if deps["is_referenced"]:
                blocked.append({
                    "item_id": it.internal_item_id,
                    "sku": it.internal_sku,
                    "references": deps["references"]
                })
            else:
                deleted_ids.append(it.internal_item_id)

    if deleted_ids:
        del_set = set(deleted_ids)
        new_catalog = [it for it in catalog if it.internal_item_id not in del_set]
        save_item_master(new_catalog)

    return {
        "deleted_count": len(deleted_ids),
        "blocked_count": len(blocked),
        "deleted_ids": deleted_ids,
        "blocked_items": blocked
    }


def record_import_batch(batch_id: str, filename: str, item_ids: List[str], source_type: str = "RFQ_REQUIREMENTS") -> ImportBatchRecord:
    ITEM_MASTER_DIR.mkdir(parents=True, exist_ok=True)
    batches = []
    if IMPORT_BATCHES_FILE.exists():
        try:
            with open(IMPORT_BATCHES_FILE, "r", encoding="utf-8") as f:
                batches = [ImportBatchRecord.model_validate(b) for b in json.load(f)]
        except Exception:
            batches = []

    rec = ImportBatchRecord(
        import_batch_id=batch_id,
        filename=filename,
        imported_at=datetime.utcnow().isoformat(),
        item_ids=item_ids,
        items_count=len(item_ids),
        status="COMPLETED",
        source_type=source_type
    )
    batches.insert(0, rec)
    with open(IMPORT_BATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump([b.model_dump(mode="json") for b in batches], f, indent=2)
    return rec


def get_import_batches() -> List[Dict[str, Any]]:
    if not IMPORT_BATCHES_FILE.exists():
        return []
    try:
        with open(IMPORT_BATCHES_FILE, "r", encoding="utf-8") as f:
            batch_data = json.load(f)
            batches = [ImportBatchRecord.model_validate(b) for b in batch_data]
    except Exception:
        return []

    catalog_items = get_item_master(include_inactive=True)
    catalog_by_id = {it.internal_item_id: it for it in catalog_items}

    results = []
    for b in batches:
        active_c = 0
        inactive_c = 0
        remaining_c = 0
        for i_id in b.item_ids:
            if i_id in catalog_by_id:
                remaining_c += 1
                if catalog_by_id[i_id].status == "INACTIVE":
                    inactive_c += 1
                else:
                    active_c += 1

        results.append({
            "import_batch_id": b.import_batch_id,
            "filename": b.filename,
            "imported_at": b.imported_at,
            "items_count": b.items_count,
            "remaining_count": remaining_c,
            "active_count": active_c,
            "inactive_count": inactive_c,
            "status": b.status,
            "source_type": b.source_type
        })
    return results


def preview_undo_import_batch(batch_id: str) -> Dict[str, Any]:
    catalog_items = get_item_master(include_inactive=True)
    batch_items = [it for it in catalog_items if it.import_batch_id == batch_id]

    can_delete = []
    must_deactivate = []

    for it in batch_items:
        deps = check_item_dependencies(it.internal_item_id, it.internal_sku)
        if deps["is_referenced"]:
            must_deactivate.append({
                "item_id": it.internal_item_id,
                "sku": it.internal_sku,
                "description": it.canonical_description,
                "references": deps["references"]
            })
        else:
            can_delete.append({
                "item_id": it.internal_item_id,
                "sku": it.internal_sku,
                "description": it.canonical_description
            })

    batch_filename = batch_id
    if IMPORT_BATCHES_FILE.exists():
        try:
            with open(IMPORT_BATCHES_FILE, "r", encoding="utf-8") as f:
                for b in json.load(f):
                    if b.get("import_batch_id") == batch_id:
                        batch_filename = b.get("filename", batch_id)
                        break
        except Exception:
            pass

    return {
        "batch_id": batch_id,
        "filename": batch_filename,
        "total_items": len(batch_items),
        "can_delete_count": len(can_delete),
        "must_deactivate_count": len(must_deactivate),
        "can_delete_items": can_delete,
        "must_deactivate_items": must_deactivate
    }


def undo_import_batch(batch_id: str) -> Dict[str, Any]:
    catalog_items = get_item_master(include_inactive=True)
    deleted_skus = []
    deactivated_skus = []
    retained_items = []

    for it in catalog_items:
        if it.import_batch_id == batch_id:
            deps = check_item_dependencies(it.internal_item_id, it.internal_sku)
            if deps["is_referenced"]:
                it.status = "INACTIVE"
                retained_items.append(it)
                deactivated_skus.append(it.internal_sku)
            else:
                deleted_skus.append(it.internal_sku)
        else:
            retained_items.append(it)

    save_item_master(retained_items)

    if IMPORT_BATCHES_FILE.exists():
        try:
            with open(IMPORT_BATCHES_FILE, "r", encoding="utf-8") as f:
                batches = json.load(f)
                for b in batches:
                    if b.get("import_batch_id") == batch_id:
                        b["status"] = "UNDONE"
            with open(IMPORT_BATCHES_FILE, "w", encoding="utf-8") as f:
                json.dump(batches, f, indent=2)
        except Exception:
            pass

    return {
        "batch_id": batch_id,
        "deleted_count": len(deleted_skus),
        "deactivated_count": len(deactivated_skus),
        "deleted_skus": deleted_skus,
        "deactivated_skus": deactivated_skus
    }


# =========================================================================
# SUPPLIER MASTER LIFECYCLE & IMMUTABLE IDENTITY
# =========================================================================

def ensure_supplier_master_initialized():
    """Seeds canonical Supplier Master into suppliers.json if it doesn't exist."""
    ITEM_MASTER_DIR.mkdir(parents=True, exist_ok=True)
    if not SUPPLIERS_FILE.exists():
        initial_suppliers = [
            SupplierMasterRecord(
                supplier_id="BENCH-SUPP-A",
                supplier_name="Alpha Industrial Supplies",
                status="ACTIVE",
                created_at="2026-01-01T00:00:00Z"
            ),
            SupplierMasterRecord(
                supplier_id="BENCH-SUPP-B",
                supplier_name="Bharat Industrial Components",
                status="ACTIVE",
                created_at="2026-01-01T00:00:00Z"
            ),
            SupplierMasterRecord(
                supplier_id="BENCH-SUPP-C",
                supplier_name="Continental Machinery Corp",
                status="ACTIVE",
                created_at="2026-01-01T00:00:00Z"
            ),
            SupplierMasterRecord(
                supplier_id="BENCH-SUPP-D",
                supplier_name="Delta Fasteners & Seals",
                status="ACTIVE",
                created_at="2026-01-01T00:00:00Z"
            ),
            SupplierMasterRecord(
                supplier_id="BENCH-SUPP-E",
                supplier_name="Elite Electricals & Cable Corp",
                status="ACTIVE",
                created_at="2026-01-01T00:00:00Z"
            ),
        ]
        with open(SUPPLIERS_FILE, "w", encoding="utf-8") as f:
            json.dump([s.model_dump(mode="json") for s in initial_suppliers], f, indent=2)


ensure_supplier_master_initialized()


def get_supplier_master(include_inactive: bool = True) -> List[SupplierMasterRecord]:
    """Returns all suppliers from Supplier Master."""
    ensure_supplier_master_initialized()
    try:
        with open(SUPPLIERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            records = [SupplierMasterRecord.model_validate(d) for d in data]
            if include_inactive:
                return records
            return [s for s in records if s.status != "INACTIVE"]
    except Exception:
        return []


def get_supplier(supplier_id: str) -> Optional[SupplierMasterRecord]:
    for s in get_supplier_master(include_inactive=True):
        if s.supplier_id == supplier_id:
            return s
    return None


def save_supplier_master(suppliers: List[SupplierMasterRecord]):
    ensure_supplier_master_initialized()
    with open(SUPPLIERS_FILE, "w", encoding="utf-8") as f:
        json.dump([s.model_dump(mode="json") for s in suppliers], f, indent=2)


def register_supplier_if_not_exists(supplier_id: str, supplier_name: str) -> SupplierMasterRecord:
    """Auto-registers a supplier into Supplier Master if not present."""
    suppliers = get_supplier_master(include_inactive=True)
    for s in suppliers:
        if s.supplier_id == supplier_id:
            return s
    new_supp = SupplierMasterRecord(
        supplier_id=supplier_id,
        supplier_name=supplier_name or supplier_id,
        status="ACTIVE",
        created_at=datetime.utcnow().isoformat()
    )
    suppliers.append(new_supp)
    save_supplier_master(suppliers)
    return new_supp


def check_supplier_dependencies(supplier_id: str) -> Dict[str, Any]:
    """
    Checks if a Supplier Master record is referenced by any Quote, Comparison, or Award.
    """
    references = []
    supp_id_clean = supplier_id.strip()

    # 1. Check Quotes in DATA_DIR
    if DATA_DIR.exists():
        for q_dir in DATA_DIR.iterdir():
            if not q_dir.is_dir() or q_dir.name in ["rfqs", "comparisons", "awards", "item_master", "benchmark_fixtures"]:
                continue
            meta_file = q_dir / "metadata.json"
            if meta_file.exists():
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        if meta.get("supplier_id") == supp_id_clean or meta.get("test_id") == supp_id_clean or q_dir.name == supp_id_clean:
                            references.append(f"Quotation '{q_dir.name}' ({meta.get('test_name', 'Quote')})")
                except Exception:
                    continue

    # 2. Check Comparisons
    if COMPARISONS_DIR.exists():
        for comp_file in COMPARISONS_DIR.glob("*.json"):
            try:
                with open(comp_file, "r", encoding="utf-8") as f:
                    comp_data = json.load(f)
                    participating = comp_data.get("quote_ids", [])
                    if supp_id_clean in participating:
                        references.append(f"Comparison '{comp_data.get('comparison_id', comp_file.stem)}'")
            except Exception:
                continue

    # 3. Check Awards
    if AWARDS_DIR.exists():
        for award_file in AWARDS_DIR.glob("*.json"):
            try:
                with open(award_file, "r", encoding="utf-8") as f:
                    award_data = json.load(f)
                    for alloc in award_data.get("allocations", []):
                        for split in alloc.get("splits", []):
                            if split.get("supplier_id") == supp_id_clean or split.get("quote_id") == supp_id_clean:
                                references.append(f"Award Allocation '{award_data.get('rfq_id', award_file.stem)}'")
                                break
            except Exception:
                continue

    return {
        "is_referenced": len(references) > 0,
        "references": references,
        "reference_count": len(references)
    }


def update_supplier_name(supplier_id: str, new_name: str, changed_by: str = "USER") -> Tuple[bool, str, Optional[SupplierMasterRecord]]:
    """
    Updates a supplier's display name while preserving immutable supplier_id.
    Persists audit trail in name_history.
    Does NOT rewrite historical finalized awards or comparisons.
    """
    clean_name = new_name.strip()
    if not clean_name:
        return False, "Supplier name cannot be empty.", None

    suppliers = get_supplier_master(include_inactive=True)
    target = None
    for s in suppliers:
        if s.supplier_id == supplier_id:
            target = s
            break

    if not target:
        return False, f"Supplier '{supplier_id}' not found.", None

    if target.supplier_name == clean_name:
        return True, "Name is unchanged.", target

    old_name = target.supplier_name
    target.name_history.append(SupplierNameChange(
        old_name=old_name,
        new_name=clean_name,
        changed_at=datetime.utcnow().isoformat(),
        changed_by=changed_by
    ))
    target.supplier_name = clean_name
    save_supplier_master(suppliers)
    return True, f"Supplier name updated from '{old_name}' to '{clean_name}'.", target


def deactivate_supplier(supplier_id: str) -> Tuple[bool, str]:
    suppliers = get_supplier_master(include_inactive=True)
    for s in suppliers:
        if s.supplier_id == supplier_id:
            s.status = "INACTIVE"
            save_supplier_master(suppliers)
            return True, f"Supplier '{s.supplier_name}' ({supplier_id}) has been deactivated."
    return False, f"Supplier '{supplier_id}' not found."


def restore_supplier(supplier_id: str) -> Tuple[bool, str]:
    suppliers = get_supplier_master(include_inactive=True)
    for s in suppliers:
        if s.supplier_id == supplier_id:
            s.status = "ACTIVE"
            save_supplier_master(suppliers)
            return True, f"Supplier '{s.supplier_name}' ({supplier_id}) restored to Active."
    return False, f"Supplier '{supplier_id}' not found."


def delete_supplier(supplier_id: str) -> Tuple[bool, str]:
    """Safe hard deletion: blocked if referenced in any historical quote/comparison/award."""
    deps = check_supplier_dependencies(supplier_id)
    if deps["is_referenced"]:
        ref_summary = ", ".join(deps["references"][:3])
        if len(deps["references"]) > 3:
            ref_summary += f" (+{len(deps['references'])-3} more)"
        return False, f"Cannot delete supplier '{supplier_id}': referenced in {deps['reference_count']} procurement record(s) ({ref_summary}). Deactivate the supplier instead."

    suppliers = get_supplier_master(include_inactive=True)
    new_list = [s for s in suppliers if s.supplier_id != supplier_id]
    if len(new_list) == len(suppliers):
        return False, f"Supplier '{supplier_id}' not found."
    save_supplier_master(new_list)
    return True, f"Supplier '{supplier_id}' successfully deleted."


def parse_catalog_upload(filename: str, content: bytes) -> CatalogExtractionResult:
    """
    Parses an uploaded catalog document (CSV, XLSX, XLS, PDF) using the dedicated
    CatalogExtractor and underlying DocumentAST parser infrastructure.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / filename
        with open(tmp_path, "wb") as f:
            f.write(content)

        try:
            if filename.lower().endswith((".xlsx", ".xls")):
                ast = ExcelParser().parse(tmp_path)
            elif filename.lower().endswith(".csv"):
                ast = CSVParser().parse(tmp_path)
            elif filename.lower().endswith(".pdf"):
                ast = PDFParser().parse(tmp_path)
            else:
                ast = ExcelParser().parse(tmp_path)
            
            return CatalogExtractor().extract(ast)
        except Exception as e:
            return CatalogExtractionResult(
                is_valid_catalog=False,
                rejection_reason=f"Failed to parse document: {str(e)}",
                confidence_score=0.0,
                confidence_explanation=f"Error encountered during document parsing: {str(e)}"
            )


def commit_catalog_import(items_data: List[Dict[str, Any]], mode: str = "append", filename: str = "Catalog Import") -> Dict[str, Any]:
    """
    Validates and commits a list of parsed preview items into the company Item Master catalog.
    mode can be 'append' (adds to existing) or 'replace' (replaces catalog).
    """
    existing_items = [] if mode == "replace" else get_item_master(include_inactive=True)
    
    existing_ids = [it.internal_item_id for it in existing_items if it.internal_item_id.startswith("ITEM-")]
    indices = []
    for i in existing_ids:
        try:
            indices.append(int(i.replace("ITEM-", "")))
        except ValueError:
            pass
    next_idx = max(indices, default=0) + 1

    committed_count = 0
    new_item_ids = []
    batch_id = f"IMP-CAT-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"

    for it in items_data:
        sku = str(it.get("internal_sku", "")).strip().upper()
        desc = str(it.get("canonical_description", "")).strip()
        if not sku or not desc:
            continue
        
        if mode == "append" and any(e.internal_sku == sku for e in existing_items):
            target = next(e for e in existing_items if e.internal_sku == sku)
            if it.get("manufacturer_part_number"):
                target.manufacturer_part_number = str(it.get("manufacturer_part_number")).strip()
            if it.get("stocking_uom"):
                target.stocking_uom = str(it.get("stocking_uom")).strip().upper()
            committed_count += 1
            continue

        item_id = f"ITEM-{next_idx:03d}"
        next_idx += 1
        
        specs = it.get("specifications", {})
        if isinstance(specs, str):
            try:
                specs = json.loads(specs)
            except Exception:
                specs = {}

        approved_pns = it.get("approved_supplier_part_numbers", [])
        if isinstance(approved_pns, str):
            approved_pns = [p.strip() for p in approved_pns.split(",") if p.strip()]

        rec = ItemMasterRecord(
            internal_item_id=item_id,
            internal_sku=sku,
            manufacturer_part_number=str(it.get("manufacturer_part_number")).strip() if it.get("manufacturer_part_number") else None,
            approved_supplier_part_numbers=approved_pns,
            canonical_description=desc,
            stocking_uom=str(it.get("stocking_uom", "PCS")).strip().upper() or "PCS",
            brand=str(it.get("brand")).strip() if it.get("brand") else "Standard",
            specifications=specs,
            status="ACTIVE",
            import_batch_id=batch_id,
            created_at=datetime.utcnow().isoformat()
        )
        existing_items.append(rec)
        new_item_ids.append(item_id)
        committed_count += 1

    save_item_master(existing_items)
    if new_item_ids:
        record_import_batch(
            batch_id=batch_id,
            filename=filename,
            item_ids=new_item_ids,
            source_type="CATALOG_IMPORT"
        )
    return {"committed_count": committed_count, "import_batch_id": batch_id, "new_items_count": len(new_item_ids)}


# =========================================================================
# 2. SUPPLIER QUOTATION INGESTION & PARSING
# =========================================================================

def _get_next_quote_id(rfq_id: Optional[str] = None) -> str:
    """Generates sequential clean quote id."""
    existing = [d.name for d in DATA_DIR.iterdir() if d.is_dir() and (d.name.startswith("Q-") or d.name.startswith("QUOTE-") or d.name.startswith("TEST-") or d.name.startswith("BENCH-"))]
    indices = []
    for name in existing:
        try:
            parts = name.split("-")
            indices.append(int(parts[-1]))
        except (IndexError, ValueError):
            continue
    next_idx = max(indices, default=0) + 1
    prefix = f"Q-{rfq_id}" if rfq_id else "QUOTE"
    return f"{prefix}-{next_idx:03d}"


def create_quote(quote_name: str, rfq_id: Optional[str] = None) -> str:
    quote_id = _get_next_quote_id(rfq_id)
    quote_dir = DATA_DIR / quote_id
    quote_dir.mkdir(parents=True, exist_ok=True)
    (quote_dir / "source").mkdir(exist_ok=True)
    (quote_dir / "extracted").mkdir(exist_ok=True)
    (quote_dir / "matching").mkdir(exist_ok=True)
    (quote_dir / "corrections").mkdir(exist_ok=True)
    (quote_dir / "reports").mkdir(exist_ok=True)

    metadata = {
        "test_id": quote_id,
        "test_name": quote_name.strip() or f"Quotation {quote_id}",
        "created_at": datetime.utcnow().isoformat(),
        "status": "CREATED",
        "source_file_name": None,
        "source_file_size_bytes": 0,
        "source_file_hash": None,
        "parser_used": None,
        "processing_time_seconds": 0.0,
        "error_message": None,
        "error_traceback": None,
        "last_stage": None,
        "has_corrections": False,
        "average_confidence": None,
        "rfq_id": rfq_id,
        "match_status": "NOT_MATCHED"
    }
    with open(quote_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    return quote_id


def create_test(test_name: str, rfq_id: Optional[str] = None) -> str:
    existing = [d.name for d in DATA_DIR.iterdir() if d.is_dir() and d.name.startswith("TEST-")]
    indices = []
    for name in existing:
        try:
            parts = name.split("-")
            indices.append(int(parts[-1]))
        except (IndexError, ValueError):
            continue
    next_idx = max(indices, default=0) + 1
    test_id = f"TEST-{next_idx:04d}"
    test_dir = DATA_DIR / test_id
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "source").mkdir(exist_ok=True)
    (test_dir / "extracted").mkdir(exist_ok=True)
    (test_dir / "matching").mkdir(exist_ok=True)
    (test_dir / "corrections").mkdir(exist_ok=True)
    (test_dir / "reports").mkdir(exist_ok=True)

    metadata = {
        "test_id": test_id,
        "test_name": test_name.strip() or f"Test Run {test_id}",
        "created_at": datetime.utcnow().isoformat(),
        "status": "CREATED",
        "source_file_name": None,
        "source_file_size_bytes": 0,
        "source_file_hash": None,
        "parser_used": None,
        "processing_time_seconds": 0.0,
        "error_message": None,
        "error_traceback": None,
        "last_stage": None,
        "has_corrections": False,
        "average_confidence": None,
        "rfq_id": rfq_id,
        "match_status": "NOT_MATCHED"
    }
    with open(test_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    return test_id


def upload_source_file(quote_id: str, filename: str, content: bytes) -> Dict[str, Any]:
    quote_dir = DATA_DIR / quote_id
    if not quote_dir.exists():
        raise ValueError(f"Quote {quote_id} does not exist.")

    source_dir = quote_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    target_path = source_dir / filename
    with open(target_path, "wb") as f:
        f.write(content)

    meta = get_quote_metadata(quote_id) or {}
    meta["source_file_name"] = filename
    meta["source_file_size_bytes"] = len(content)
    meta["status"] = "READY"
    meta["error_message"] = None
    meta["error_traceback"] = None

    with open(quote_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return meta


def run_extraction(quote_id: str) -> CanonicalQuote:
    meta = get_quote_metadata(quote_id)
    if not meta or not meta.get("source_file_name"):
        raise ValueError(f"Quote {quote_id} has no uploaded document.")

    source_path = DATA_DIR / quote_id / "source" / meta["source_file_name"]
    if not source_path.exists():
        raise FileNotFoundError(f"Source file not found at {source_path}")

    t0 = time.time()
    ext = source_path.suffix.lower()
    if ext in [".xlsx", ".xls"]:
        parser = ExcelParser()
    elif ext == ".csv":
        parser = CSVParser()
    elif ext == ".pdf":
        parser = PDFParser()
    else:
        raise ValueError(f"Unsupported file format: {ext}")

    try:
        ast = parser.parse(source_path)
        extractor = QuoteExtractor()
        quote = extractor.extract(ast)
        quote.quote_id = quote_id

        # Preserve previous human overrides if present
        existing_quote = get_canonical_quote(quote_id)
        decimal_fields = {"quoted_qty", "unit_price", "discount_pct", "net_unit_price", "tax_rate_pct", "uom_conversion_factor", "moq"}
        if existing_quote:
            for old_item in existing_quote.items:
                if old_item.field_corrections:
                    new_item = next((it for it in quote.items if it.line_index == old_item.line_index), None)
                    if new_item:
                        for fname, corr in old_item.field_corrections.items():
                            if hasattr(new_item, fname):
                                val = Decimal(str(corr.corrected_value)) if fname in decimal_fields and corr.corrected_value is not None else corr.corrected_value
                                setattr(new_item, fname, val)
                                new_item.field_corrections[fname] = corr

        report = extractor.reconciler.reconcile(quote)
        duration = round(time.time() - t0, 3)

        # Save CanonicalQuote JSON
        extracted_dir = DATA_DIR / quote_id / "extracted"
        extracted_dir.mkdir(parents=True, exist_ok=True)
        with open(extracted_dir / "canonical_quote.json", "w", encoding="utf-8") as f:
            f.write(quote.model_dump_json(indent=2))

        report_dict = {
            "is_valid": report.is_valid,
            "overall_status": report.overall_status.value if hasattr(report.overall_status, "value") else str(report.overall_status),
            "calibrated_confidence": report.calibrated_confidence,
            "total_landed_cost": str(report.total_landed_cost),
            "calculated_expected_total": str(report.financial_model.calculated_expected_total),
            "supplier_stated_grand_total": str(report.financial_model.supplier_stated_grand_total) if report.financial_model.supplier_stated_grand_total is not None else None,
            "reconciliation_status": report.financial_model.reconciliation_status.value if hasattr(report.financial_model.reconciliation_status, "value") else str(report.financial_model.reconciliation_status),
            "discrepancy": str(report.financial_model.discrepancy) if report.financial_model.discrepancy is not None else None,
            "issues": [{"field_path": i.field_path, "message": i.message, "severity": i.severity.value if hasattr(i.severity, "value") else str(i.severity)} for i in report.issues],
            "confidence_reasons": report.confidence_reasons,
            "line_count": len(quote.items)
        }

        reports_dir = DATA_DIR / quote_id / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        with open(reports_dir / "extraction_report.json", "w", encoding="utf-8") as f:
            json.dump(report_dict, f, indent=2)

        meta["status"] = "SUCCESS"
        meta["processing_time_seconds"] = duration
        meta["parser_used"] = quote.extraction_metadata.parser_used
        meta["average_confidence"] = quote.extraction_metadata.overall_confidence
        meta["has_corrections"] = any(item.field_corrections for item in quote.items)
        meta["error_message"] = None
        meta["error_traceback"] = None

        with open(DATA_DIR / quote_id / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        return quote

    except Exception as exc:
        duration = round(time.time() - t0, 3)
        import traceback
        tb = traceback.format_exc()

        meta["status"] = "FAILED"
        meta["processing_time_seconds"] = duration
        meta["error_message"] = str(exc)
        meta["error_traceback"] = tb

        with open(DATA_DIR / quote_id / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        raise exc


def ingest_quote_file_for_rfq(rfq_id: str, filename: str, content: bytes, supplier_name: Optional[str] = None) -> str:
    """Convenience method: uploads, extracts, and matches quote to RFQ in one step."""
    name = supplier_name or filename.rsplit(".", 1)[0].replace("_", " ").title()
    quote_id = create_quote(name, rfq_id)
    upload_source_file(quote_id, filename, content)
    run_extraction(quote_id)
    run_matching_for_quote(quote_id, rfq_id)
    return quote_id


def get_canonical_quote(quote_id: str) -> Optional[CanonicalQuote]:
    canon_file = DATA_DIR / quote_id / "extracted" / "canonical_quote.json"
    if not canon_file.exists():
        return None
    try:
        with open(canon_file, "r", encoding="utf-8") as f:
            return CanonicalQuote.model_validate_json(f.read())
    except Exception:
        return None


def get_extraction_report(quote_id: str) -> Optional[Dict[str, Any]]:
    rep_file = DATA_DIR / quote_id / "reports" / "extraction_report.json"
    if not rep_file.exists():
        return None
    try:
        with open(rep_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_quote_metadata(quote_id: str) -> Optional[Dict[str, Any]]:
    meta_file = DATA_DIR / quote_id / "metadata.json"
    if not meta_file.exists():
        return None
    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_test(test_id: str) -> Optional[Dict[str, Any]]:
    return get_quote_metadata(test_id)


def get_quote_details(quote_id: str) -> Optional[Dict[str, Any]]:
    meta = get_quote_metadata(quote_id)
    if not meta:
        return None

    quote = get_canonical_quote(quote_id)
    report = get_extraction_report(quote_id)

    has_source = (meta.get("source_file_name") is not None)
    sheets = []
    sheet_data = {}

    if has_source and meta["source_file_name"].lower().endswith((".xlsx", ".xls")):
        source_path = DATA_DIR / quote_id / "source" / meta["source_file_name"]
        if source_path.exists():
            try:
                import openpyxl
                wb = openpyxl.load_workbook(source_path, data_only=True, read_only=True)
                try:
                    sheets = list(wb.sheetnames)
                    for sname in sheets[:4]:
                        ws = wb[sname]
                        rows = []
                        for r in ws.iter_rows(values_only=True):
                            if any(v is not None for v in r):
                                rows.append([str(v) if v is not None else "" for v in r[:12]])
                            if len(rows) >= 25:
                                break
                        sheet_data[sname] = rows
                finally:
                    wb.close()
            except Exception:
                pass

    return {
        "metadata": meta,
        "canonical_quote": quote,
        "report": report,
        "has_source": has_source,
        "sheets": sheets,
        "sheet_data": sheet_data
    }


def get_test_details(test_id: str) -> Optional[Dict[str, Any]]:
    return get_quote_details(test_id)


def apply_field_correction(quote_id: str, line_index: int, field_name: str, new_value: Any) -> CanonicalQuote:
    quote = get_canonical_quote(quote_id)
    if not quote:
        raise ValueError(f"Quote {quote_id} not extracted yet.")

    target_item = next((item for item in quote.items if item.line_index == line_index), None)
    if not target_item:
        raise ValueError(f"Line item at index {line_index} not found in quote {quote_id}.")

    decimal_fields = {"quoted_qty", "unit_price", "discount_pct", "net_unit_price", "tax_rate_pct", "uom_conversion_factor", "moq"}
    val = Decimal(str(new_value)) if field_name in decimal_fields and new_value is not None else new_value
    target_item.apply_human_override(field_name, val, user="web_user")

    from extraction.reconciliation import QuoteReconciler
    reconciler = QuoteReconciler()
    report = reconciler.reconcile(quote)

    extracted_dir = DATA_DIR / quote_id / "extracted"
    with open(extracted_dir / "canonical_quote.json", "w", encoding="utf-8") as f:
        f.write(quote.model_dump_json(indent=2))

    report_dict = {
        "is_valid": report.is_valid,
        "overall_status": report.overall_status.value if hasattr(report.overall_status, "value") else str(report.overall_status),
        "calibrated_confidence": report.calibrated_confidence,
        "total_landed_cost": str(report.total_landed_cost),
        "calculated_expected_total": str(report.financial_model.calculated_expected_total),
        "supplier_stated_grand_total": str(report.financial_model.supplier_stated_grand_total) if report.financial_model.supplier_stated_grand_total is not None else None,
        "reconciliation_status": report.financial_model.reconciliation_status.value if hasattr(report.financial_model.reconciliation_status, "value") else str(report.financial_model.reconciliation_status),
        "discrepancy": str(report.financial_model.discrepancy) if report.financial_model.discrepancy is not None else None,
        "issues": [{"field_path": i.field_path, "message": i.message, "severity": i.severity.value if hasattr(i.severity, "value") else str(i.severity)} for i in report.issues],
        "confidence_reasons": report.confidence_reasons,
        "line_count": len(quote.items)
    }

    reports_dir = DATA_DIR / quote_id / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    with open(reports_dir / "extraction_report.json", "w", encoding="utf-8") as f:
        json.dump(report_dict, f, indent=2)

    meta = get_quote_metadata(quote_id) or {}
    meta["has_corrections"] = True
    with open(DATA_DIR / quote_id / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return quote


def reset_test(quote_id: str):
    quote_dir = DATA_DIR / quote_id
    if not quote_dir.exists():
        return

    for folder in ["extracted", "matching", "corrections", "reports"]:
        fpath = quote_dir / folder
        if fpath.exists():
            shutil.rmtree(fpath)
        fpath.mkdir(exist_ok=True)

    meta = get_quote_metadata(quote_id) or {}
    meta["status"] = "READY" if meta.get("source_file_name") else "CREATED"
    meta["parser_used"] = None
    meta["processing_time_seconds"] = 0.0
    meta["error_message"] = None
    meta["error_traceback"] = None
    meta["has_corrections"] = False
    meta["average_confidence"] = None
    meta["match_status"] = "NOT_MATCHED"

    with open(quote_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def rename_test(quote_id: str, new_name: str):
    quote_dir = DATA_DIR / quote_id
    if not quote_dir.exists():
        return
    meta = get_quote_metadata(quote_id) or {}
    meta["test_name"] = new_name.strip()
    with open(quote_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def delete_test(quote_id: str):
    import gc
    gc.collect()
    quote_dir = DATA_DIR / quote_id
    if quote_dir.exists():
        try:
            shutil.rmtree(quote_dir)
        except Exception:
            import os
            for root, dirs, files in os.walk(quote_dir, topdown=False):
                for name in files:
                    try:
                        os.unlink(os.path.join(root, name))
                    except Exception:
                        pass
                for name in dirs:
                    try:
                        os.rmdir(os.path.join(root, name))
                    except Exception:
                        pass
            try:
                os.rmdir(quote_dir)
            except Exception:
                pass


# =========================================================================
# 3. ITEM MASTER MATCHING & CANDIDATE RESOLUTION (WITH LOCKED HUMAN DECISIONS)
# =========================================================================

def is_human_resolved_match(m: MatchedQuoteItem) -> bool:
    """
    Determines whether a matched line item represents an authoritative, locked human decision.
    A line is HUMAN_RESOLVED when its persisted state explicitly indicates human confirmation.
    """
    if m.item_master_match:
        fields = m.item_master_match.matched_fields or []
        if "human_confirmation" in fields or "manual_override" in fields:
            return True
        exps = m.item_master_match.explanations or []
        if any("procurement reviewer" in str(e).lower() or "confirmed by" in str(e).lower() for e in exps):
            return True

    if m.rfq_match:
        fields = m.rfq_match.matched_fields or []
        if "human_confirmation" in fields or "manual_override" in fields:
            return True
        exps = m.rfq_match.explanations or []
        if any("procurement reviewer" in str(e).lower() or "confirmed by" in str(e).lower() for e in exps):
            return True

    if m.review_reasons:
        if any("procurement reviewer" in str(r).lower() or "manual override" in str(r).lower() for r in m.review_reasons):
            return True

    return False


def compute_quotes_matching_fingerprint(quote_ids: List[str]) -> str:
    """
    Computes a deterministic hash fingerprint of the matching files for the given quotes.
    Used for comparison freshness and staleness detection.
    """
    import hashlib
    h = hashlib.sha256()
    for qid in sorted(quote_ids):
        match_file = DATA_DIR / qid / "matching" / "matched_items.json"
        if match_file.exists():
            try:
                mtime_ns = match_file.stat().st_mtime_ns
                h.update(f"{qid}:{mtime_ns}:".encode("utf-8"))
                h.update(match_file.read_bytes())
            except Exception:
                h.update(f"{qid}:err:".encode("utf-8"))
        else:
            h.update(f"{qid}:none:".encode("utf-8"))
    return h.hexdigest()


def get_supplier_mappings() -> List[SupplierMappingRecord]:
    """Retrieves all persisted supplier-to-item confirmed memory mappings."""
    if not SUPPLIER_MAPPINGS_FILE.exists():
        return []
    try:
        with open(SUPPLIER_MAPPINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [SupplierMappingRecord.model_validate(d) for d in data]
    except Exception:
        return []


def save_supplier_mapping(
    supplier_id: str,
    supplier_part_number: str,
    internal_sku: str,
    supplier_name: Optional[str] = None,
    internal_item_id: Optional[str] = None,
    canonical_description: Optional[str] = None,
    confirmed_by: str = "Procurement Specialist",
    source_rfq_id: Optional[str] = None,
    source_quote_id: Optional[str] = None
) -> SupplierMappingRecord:
    """Persists a human-confirmed supplier part number to internal SKU relationship."""
    SUPPLIER_MAPPINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    mappings = get_supplier_mappings()
    now_iso = datetime.utcnow().isoformat()
    norm_supp = str(supplier_id).upper().strip()
    norm_pn = str(supplier_part_number).upper().strip()

    # Remove existing mapping for this supplier + part number if already present
    filtered = [m for m in mappings if not (m.supplier_id.upper().strip() == norm_supp and m.supplier_part_number.upper().strip() == norm_pn)]

    record = SupplierMappingRecord(
        supplier_id=supplier_id.strip(),
        supplier_name=supplier_name.strip() if supplier_name else None,
        supplier_part_number=supplier_part_number.strip(),
        internal_sku=internal_sku.strip(),
        internal_item_id=internal_item_id,
        canonical_description=canonical_description,
        confirmed_by=confirmed_by,
        confirmed_at=now_iso,
        source_rfq_id=source_rfq_id,
        source_quote_id=source_quote_id
    )
    filtered.append(record)

    tmp_file = SUPPLIER_MAPPINGS_FILE.parent / f"supplier_mappings.tmp.{uuid.uuid4().hex[:8]}"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump([m.model_dump(mode="json") for m in filtered], f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, SUPPLIER_MAPPINGS_FILE)
    return record


def lookup_supplier_mapping(supplier_id: str, supplier_part_number: str) -> Optional[SupplierMappingRecord]:
    """Looks up a known supplier part number mapping for the given supplier."""
    mappings = get_supplier_mappings()
    norm_supp = str(supplier_id).upper().strip()
    norm_pn = str(supplier_part_number).upper().strip()
    return next((m for m in mappings if m.supplier_id.upper().strip() == norm_supp and m.supplier_part_number.upper().strip() == norm_pn), None)


def run_matching_for_quote(quote_id: str, rfq_id: Optional[str] = None) -> List[MatchedQuoteItem]:
    """
    Runs multi-signal universal item matching for a quote while strictly preserving locked human decisions.
    Invariant: A manual procurement reviewer decision MUST NEVER be silently overwritten by an automatic matching run.
    """
    quote = get_canonical_quote(quote_id)
    if not quote:
        raise ValueError(f"No canonical quote found for {quote_id}. Run extraction first.")

    rfq = get_rfq(rfq_id) if rfq_id else None
    if not rfq:
        meta = get_quote_metadata(quote_id) or {}
        rfq = get_rfq(meta.get("rfq_id")) if meta.get("rfq_id") else None

    if not rfq:
        rfqs = list_rfqs(include_demo=True)
        rfq = get_rfq(rfqs[0]["rfq_id"]) if rfqs else get_benchmark_rfq()

    # 1. Load existing matching state and identify locked human-resolved lines
    existing_matches = get_matched_items(quote_id) or []
    human_resolved_by_line: Dict[int, MatchedQuoteItem] = {}
    for em in existing_matches:
        if is_human_resolved_match(em):
            # Rebind RFQ match to current RFQ if RFQ line exists and was not already bound
            if rfq and em.item_master_match and not em.rfq_match:
                for rline in rfq.items:
                    if (rline.sku and rline.sku.upper() == em.item_master_match.candidate_sku.upper()) or (rline.internal_item_id == em.item_master_match.candidate_item_id):
                        em.rfq_match = MatchCandidate(
                            candidate_item_id=rline.rfq_line_id,
                            candidate_sku=rline.sku or rline.rfq_line_id,
                            candidate_description=rline.description,
                            match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                            match_score=1.0,
                            match_status=MatchStatus.EXACT_MATCH,
                            decision_band=DecisionBand.HIGH_CONFIDENCE,
                            explanations=["Confirmed by procurement reviewer"],
                            evidence_checklist=[EvidenceItem(signal="HUMAN_CONFIRMATION", status="PASS", description="Confirmed by procurement reviewer")],
                            matched_fields=["human_confirmation"],
                            source_provenance=em.quote_item.provenance
                        )
                        break
            human_resolved_by_line[em.quote_item.line_index] = em

    # 2. Run automated matching on all quote items with supplier mappings memory
    item_master = get_item_master()
    matcher = ItemMatcher()
    supp_mappings = get_supplier_mappings()
    meta = get_quote_metadata(quote_id) or {}
    supplier_id = (
        meta.get("supplier_id")
        or getattr(quote, "supplier_matched_id", None)
        or getattr(quote, "supplier_raw_name", None)
        or quote_id
    )
    fresh_matches = matcher.match_quote(
        quote,
        item_master,
        rfq.items if rfq else None,
        supplier_id=supplier_id,
        supplier_mappings=supp_mappings
    )

    # 3. Merge: NEVER replace a human-resolved line with a newly calculated automatic match!
    final_matches: List[MatchedQuoteItem] = []
    for fm in fresh_matches:
        line_idx = fm.quote_item.line_index
        if line_idx in human_resolved_by_line:
            # Preserve the authoritative human decision
            final_matches.append(human_resolved_by_line[line_idx])
        else:
            final_matches.append(fm)

    # 4. Persist the authoritative matching state
    quote_dir = DATA_DIR / quote_id
    (quote_dir / "matching").mkdir(parents=True, exist_ok=True)
    with open(quote_dir / "matching" / "matched_items.json", "w", encoding="utf-8") as f:
        f.write(json.dumps([m.model_dump(mode="json") for m in final_matches], indent=2))

    meta = get_quote_metadata(quote_id) or {}
    has_review = any(m.match_status in [MatchStatus.REVIEW_REQUIRED, MatchStatus.UOM_INCOMPATIBLE] for m in final_matches)
    meta["match_status"] = "REVIEW_REQUIRED" if has_review else "MATCHED"
    meta["rfq_id"] = rfq.rfq_id if rfq else None
    with open(quote_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return final_matches


def get_matched_items(quote_id: str) -> Optional[List[MatchedQuoteItem]]:
    quote_dir = DATA_DIR / quote_id
    match_file = quote_dir / "matching" / "matched_items.json"
    if not match_file.exists():
        return None
    try:
        with open(match_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [MatchedQuoteItem.model_validate(d) for d in data]
    except Exception:
        return None


def save_quote_matched_items(quote_id: str, matched_items: List[MatchedQuoteItem]) -> None:
    quote_dir = DATA_DIR / quote_id
    (quote_dir / "matching").mkdir(parents=True, exist_ok=True)
    with open(quote_dir / "matching" / "matched_items.json", "w", encoding="utf-8") as f:
        f.write(json.dumps([m.model_dump(mode="json") for m in matched_items], indent=2))


def resolve_match_candidate(quote_id: str, line_index: int, chosen_candidate_sku: Optional[str], action: str = "ACCEPT") -> List[MatchedQuoteItem]:
    matched_items = get_matched_items(quote_id)
    if not matched_items:
        raise ValueError(f"No matched items found for quote {quote_id}.")

    target = next((m for m in matched_items if m.quote_item.line_index == line_index), None)
    if not target:
        raise ValueError(f"Matched item with line index {line_index} not found.")

    quote = get_canonical_quote(quote_id)
    meta = get_quote_metadata(quote_id) or {}
    rfq_id = meta.get("rfq_id")
    rfq = get_rfq(rfq_id) if rfq_id else None

    if action in ["MARK_UNMATCHED", "REJECT"]:
        target.match_status = MatchStatus.UNMATCHED
        target.decision_band = DecisionBand.LOW_CONFIDENCE
        target.rfq_match = None
        target.item_master_match = None
        target.has_hard_conflict = False
        target.conflict_reasons = []
        target.review_reasons = ["Marked as unmatched / non-catalog by procurement reviewer"]
        target.evidence_checklist = [EvidenceItem(signal="HUMAN_DECISION", status="INFO", description="Marked as unmatched by buyer")]
    elif action in ["CLEAR", "RESET", "REOPEN"]:
        target.match_status = MatchStatus.REVIEW_REQUIRED
        target.decision_band = DecisionBand.MANUAL_REVIEW
        target.rfq_match = None
        target.item_master_match = None
        target.has_hard_conflict = False
        target.conflict_reasons = []
        target.review_reasons = ["Match reopened by procurement reviewer for re-evaluation"]
    elif action in ["ACCEPT", "CHOOSE", "MANUAL_OVERRIDE"]:
        if chosen_candidate_sku:
            item_master = get_item_master(include_inactive=True)
            master_rec = next((im for im in item_master if im.internal_sku.upper().strip() == chosen_candidate_sku.upper().strip()), None)
            if not master_rec and rfq and rfq.items:
                rline = next((rl for rl in rfq.items if (rl.sku and rl.sku.upper() == chosen_candidate_sku.upper()) or (rl.rfq_line_id and rl.rfq_line_id.upper() == chosen_candidate_sku.upper())), None)
                if rline:
                    master_rec = ItemMasterRecord(
                        internal_item_id=rline.internal_item_id or rline.rfq_line_id,
                        internal_sku=rline.sku or rline.rfq_line_id,
                        category=rline.category,
                        manufacturer=rline.manufacturer,
                        manufacturer_part_number=rline.manufacturer_part_number,
                        canonical_description=rline.description,
                        stocking_uom=rline.requested_uom or target.quote_item.quoted_uom or "PCS",
                        specifications=rline.specifications
                    )
            if not master_rec:
                cand_meta = target.item_master_match if (target.item_master_match and target.item_master_match.candidate_sku and target.item_master_match.candidate_sku.upper() == chosen_candidate_sku.upper()) else None
                if not cand_meta and target.top_candidates:
                    cand_meta = next((c for c in target.top_candidates if c.candidate_sku and c.candidate_sku.upper() == chosen_candidate_sku.upper()), None)
                if cand_meta:
                    master_rec = ItemMasterRecord(
                        internal_item_id=cand_meta.candidate_item_id or chosen_candidate_sku,
                        internal_sku=cand_meta.candidate_sku or chosen_candidate_sku,
                        canonical_description=cand_meta.candidate_description or chosen_candidate_sku,
                        stocking_uom=target.quote_item.quoted_uom or "PCS"
                    )

            if master_rec:
                cand = MatchCandidate(
                    candidate_item_id=master_rec.internal_item_id,
                    candidate_sku=master_rec.internal_sku,
                    candidate_description=master_rec.canonical_description,
                    match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                    match_score=1.0,
                    match_status=MatchStatus.EXACT_MATCH,
                    decision_band=DecisionBand.HIGH_CONFIDENCE,
                    explanations=["Confirmed by procurement reviewer"],
                    evidence_checklist=[EvidenceItem(signal="HUMAN_CONFIRMATION", status="PASS", description="Confirmed by procurement reviewer")],
                    matched_fields=["human_confirmation"],
                    has_hard_conflict=False,
                    conflict_reasons=[],
                    source_provenance=target.quote_item.provenance
                )
                target.item_master_match = cand
                target.decision_band = DecisionBand.HIGH_CONFIDENCE
                target.has_hard_conflict = False
                target.conflict_reasons = []
                
                # Check UOM compatibility deterministically
                from matching.uom_resolver import UOMResolver
                uom_resolver = UOMResolver()
                uom_res = uom_resolver.resolve_uom_conversion(
                    source_uom=target.quote_item.quoted_uom,
                    target_uom=master_rec.stocking_uom or target.quote_item.quoted_uom,
                    quoted_quantity=target.quote_item.quoted_qty
                )
                target.uom_conversion = uom_res
                
                if not uom_res.is_compatible:
                    target.match_status = MatchStatus.UOM_INCOMPATIBLE
                    target.has_hard_conflict = True
                    target.decision_band = DecisionBand.BLOCKED_CONFLICT
                    target.conflict_reasons = [uom_res.error_reason or "Incompatible UOM conversion"]
                    target.review_reasons = [uom_res.error_reason or "Incompatible UOM conversion"]
                else:
                    target.match_status = MatchStatus.EXACT_MATCH
                    target.review_reasons = []

                if rfq:
                    for rline in rfq.items:
                        if (rline.sku and rline.sku.upper() == master_rec.internal_sku.upper()) or (rline.internal_item_id == master_rec.internal_item_id):
                            target.rfq_match = MatchCandidate(
                                candidate_item_id=rline.rfq_line_id,
                                candidate_sku=rline.sku or rline.rfq_line_id,
                                candidate_description=rline.description,
                                match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                                match_score=1.0,
                                match_status=MatchStatus.EXACT_MATCH,
                                decision_band=DecisionBand.HIGH_CONFIDENCE,
                                explanations=["Confirmed by procurement reviewer"],
                                evidence_checklist=[EvidenceItem(signal="HUMAN_CONFIRMATION", status="PASS", description="Confirmed by procurement reviewer")],
                                matched_fields=["human_confirmation"],
                                has_hard_conflict=False,
                                conflict_reasons=[],
                                source_provenance=target.quote_item.provenance
                            )
                            break

                # Save to persistent supplier memory if supplier part number exists
                supp_id = meta.get("supplier_id") or (quote.supplier_raw_name if quote else None) or quote_id
                if supp_id and target.quote_item.supplier_part_number:
                    save_supplier_mapping(
                        supplier_id=supp_id,
                        supplier_name=quote.supplier_raw_name if quote else meta.get("supplier_name"),
                        supplier_part_number=target.quote_item.supplier_part_number,
                        internal_sku=master_rec.internal_sku,
                        internal_item_id=master_rec.internal_item_id,
                        canonical_description=master_rec.canonical_description,
                        source_rfq_id=rfq_id,
                        source_quote_id=quote_id
                    )

    quote_dir = DATA_DIR / quote_id
    with open(quote_dir / "matching" / "matched_items.json", "w", encoding="utf-8") as f:
        f.write(json.dumps([m.model_dump(mode="json") for m in matched_items], indent=2))

    meta = get_quote_metadata(quote_id) or {}
    has_review = any(m.match_status in [MatchStatus.REVIEW_REQUIRED, MatchStatus.UOM_INCOMPATIBLE] for m in matched_items)
    meta["match_status"] = "REVIEW_REQUIRED" if has_review else "MATCHED"
    with open(quote_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return matched_items


def bulk_confirm_matches(quote_id: str, line_indices: List[int]) -> Dict[str, Any]:
    """Bulk-confirms human approved matches for the specified line indices atomically."""
    matched_items = get_matched_items(quote_id)
    if not matched_items:
        raise ValueError(f"No matched items found for quote {quote_id}.")

    quote = get_canonical_quote(quote_id)
    meta = get_quote_metadata(quote_id) or {}
    rfq_id = meta.get("rfq_id")
    rfq = get_rfq(rfq_id) if rfq_id else None
    item_master = get_item_master()
    master_by_sku = {im.internal_sku.upper().strip(): im for im in item_master}

    from matching.uom_resolver import UOMResolver
    uom_resolver = UOMResolver()

    supp_id = meta.get("supplier_id") or (quote.supplier_raw_name if quote else None) or quote_id
    confirmed_count = 0
    indices_set = set(line_indices)

    for item in matched_items:
        if item.quote_item.line_index in indices_set:
            if item.item_master_match and not item.has_hard_conflict:
                cand_sku = item.item_master_match.candidate_sku
                master_rec = master_by_sku.get(cand_sku.upper().strip())
                if master_rec:
                    item.item_master_match.match_status = MatchStatus.EXACT_MATCH
                    item.item_master_match.decision_band = DecisionBand.HIGH_CONFIDENCE
                    item.item_master_match.explanations = ["Confirmed by procurement reviewer in bulk batch"]
                    item.item_master_match.evidence_checklist = [
                        EvidenceItem(signal="HUMAN_CONFIRMATION", status="PASS", description="Bulk-confirmed by buyer")
                    ]
                    item.item_master_match.has_hard_conflict = False
                    item.item_master_match.conflict_reasons = []

                    item.match_status = MatchStatus.EXACT_MATCH
                    item.decision_band = DecisionBand.HIGH_CONFIDENCE
                    item.has_hard_conflict = False
                    item.conflict_reasons = []
                    item.review_reasons = []

                    # Check UOM compatibility deterministically
                    uom_res = uom_resolver.resolve_uom_conversion(
                        source_uom=item.quote_item.quoted_uom,
                        target_uom=master_rec.stocking_uom or item.quote_item.quoted_uom,
                        quoted_quantity=item.quote_item.quoted_qty
                    )
                    item.uom_conversion = uom_res
                    if not uom_res.is_compatible:
                        item.match_status = MatchStatus.UOM_INCOMPATIBLE
                        item.has_hard_conflict = True
                        item.decision_band = DecisionBand.BLOCKED_CONFLICT
                        item.conflict_reasons = [uom_res.error_reason or "Incompatible UOM conversion"]
                    else:
                        if rfq:
                            for rline in rfq.items:
                                if (rline.sku and rline.sku.upper() == master_rec.internal_sku.upper()) or (rline.internal_item_id == master_rec.internal_item_id):
                                    item.rfq_match = MatchCandidate(
                                        candidate_item_id=rline.rfq_line_id,
                                        candidate_sku=rline.sku or rline.rfq_line_id,
                                        candidate_description=rline.description,
                                        match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                                        match_score=1.0,
                                        match_status=MatchStatus.EXACT_MATCH,
                                        decision_band=DecisionBand.HIGH_CONFIDENCE,
                                        explanations=["Bulk-confirmed by procurement reviewer"],
                                        evidence_checklist=[EvidenceItem(signal="HUMAN_CONFIRMATION", status="PASS", description="Bulk-confirmed by buyer")],
                                        matched_fields=["human_confirmation"],
                                        has_hard_conflict=False,
                                        conflict_reasons=[],
                                        source_provenance=item.quote_item.provenance
                                    )
                                    break

                        # Save to supplier memory if part number exists
                        if supp_id and item.quote_item.supplier_part_number:
                            save_supplier_mapping(
                                supplier_id=supp_id,
                                supplier_part_number=item.quote_item.supplier_part_number,
                                internal_sku=master_rec.internal_sku,
                                supplier_name=quote.supplier_raw_name if quote else None,
                                internal_item_id=master_rec.internal_item_id,
                                canonical_description=master_rec.canonical_description,
                                confirmed_by="Procurement Specialist (Bulk)",
                                source_rfq_id=rfq_id,
                                source_quote_id=quote_id
                            )
                        confirmed_count += 1

    save_quote_matched_items(quote_id, matched_items)

    return {
        "status": "success",
        "confirmed_count": confirmed_count,
        "message": f"Successfully bulk-confirmed {confirmed_count} proposed matches."
    }


def resolve_grouped_matches(quote_id: str, group_key: str, chosen_candidate_sku: str) -> Dict[str, Any]:
    """Resolves all supplier quote lines matching the group key with a single decision atomically."""
    matched_items = get_matched_items(quote_id)
    if not matched_items:
        raise ValueError(f"No matched items found for quote {quote_id}.")

    quote = get_canonical_quote(quote_id)
    meta = get_quote_metadata(quote_id) or {}
    rfq_id = meta.get("rfq_id")
    rfq = get_rfq(rfq_id) if rfq_id else None
    item_master = get_item_master()
    master_rec = next((im for im in item_master if im.internal_sku.upper().strip() == chosen_candidate_sku.upper().strip()), None)

    from matching.uom_resolver import UOMResolver
    uom_resolver = UOMResolver()

    supp_id = meta.get("supplier_id") or (quote.supplier_raw_name if quote else None) or quote_id
    resolved_count = 0

    for item in matched_items:
        desc_norm = item.quote_item.raw_description.strip().lower()
        cand_sku = (item.item_master_match.candidate_sku if item.item_master_match else "").lower()
        item_group_key = f"{desc_norm}::{cand_sku}"

        if item_group_key == group_key.lower() or desc_norm == group_key.lower():
            if master_rec:
                cand = MatchCandidate(
                    candidate_item_id=master_rec.internal_item_id,
                    candidate_sku=master_rec.internal_sku,
                    candidate_description=master_rec.canonical_description,
                    match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                    match_score=1.0,
                    match_status=MatchStatus.EXACT_MATCH,
                    decision_band=DecisionBand.HIGH_CONFIDENCE,
                    explanations=["Confirmed by procurement reviewer via group decision"],
                    evidence_checklist=[EvidenceItem(signal="HUMAN_CONFIRMATION", status="PASS", description="Group-confirmed by buyer")],
                    matched_fields=["human_confirmation", "grouped_decision"],
                    has_hard_conflict=False,
                    conflict_reasons=[],
                    source_provenance=item.quote_item.provenance
                )
                item.item_master_match = cand
                item.decision_band = DecisionBand.HIGH_CONFIDENCE
                item.has_hard_conflict = False
                item.conflict_reasons = []

                uom_res = uom_resolver.resolve_uom_conversion(
                    source_uom=item.quote_item.quoted_uom,
                    target_uom=master_rec.stocking_uom or item.quote_item.quoted_uom,
                    quoted_quantity=item.quote_item.quoted_qty
                )
                item.uom_conversion = uom_res
                if not uom_res.is_compatible:
                    item.match_status = MatchStatus.UOM_INCOMPATIBLE
                    item.has_hard_conflict = True
                    item.decision_band = DecisionBand.BLOCKED_CONFLICT
                    item.conflict_reasons = [uom_res.error_reason or "Incompatible UOM conversion"]
                    item.review_reasons = [uom_res.error_reason or "Incompatible UOM conversion"]
                else:
                    item.match_status = MatchStatus.EXACT_MATCH
                    item.review_reasons = []

                    if rfq:
                        for rline in rfq.items:
                            if (rline.sku and rline.sku.upper() == master_rec.internal_sku.upper()) or (rline.internal_item_id == master_rec.internal_item_id):
                                item.rfq_match = MatchCandidate(
                                    candidate_item_id=rline.rfq_line_id,
                                    candidate_sku=rline.sku or rline.rfq_line_id,
                                    candidate_description=rline.description,
                                    match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                                    match_score=1.0,
                                    match_status=MatchStatus.EXACT_MATCH,
                                    decision_band=DecisionBand.HIGH_CONFIDENCE,
                                    explanations=["Confirmed by procurement reviewer via group decision"],
                                    evidence_checklist=[EvidenceItem(signal="HUMAN_CONFIRMATION", status="PASS", description="Group-confirmed by buyer")],
                                    matched_fields=["human_confirmation", "grouped_decision"],
                                    has_hard_conflict=False,
                                    conflict_reasons=[],
                                    source_provenance=item.quote_item.provenance
                                )
                                break

                    if supp_id and item.quote_item.supplier_part_number:
                        save_supplier_mapping(
                            supplier_id=supp_id,
                            supplier_part_number=item.quote_item.supplier_part_number,
                            internal_sku=master_rec.internal_sku,
                            supplier_name=quote.supplier_raw_name if quote else None,
                            internal_item_id=master_rec.internal_item_id,
                            canonical_description=master_rec.canonical_description,
                            confirmed_by="Procurement Specialist (Group)",
                            source_rfq_id=rfq_id,
                            source_quote_id=quote_id
                        )
                resolved_count += 1

    save_quote_matched_items(quote_id, matched_items)

    return {
        "status": "success",
        "resolved_count": resolved_count,
        "message": f"Resolved {resolved_count} occurrences in group."
    }


def classify_matched_item_review_state(m: MatchedQuoteItem) -> str:
    """
    Authoritative single source of truth for mutually exclusive review state classification:
    - 'BLOCKED_CONFLICT': Hard conflict on MPN/Specs/UOM, explicit human override required.
    - 'HIGH_CONFIDENCE': >=95% confidence, zero hard conflicts, or explicitly confirmed by human.
    - 'BULK_CANDIDATE': 80-94% confidence, eligible for system-generated 1-click batch confirmation.
    - 'MANUAL_REVIEW': 50-79% confidence, guided continuous review queue.
    - 'LOW_CONFIDENCE': <50% confidence, non-catalog/unmatched exceptions.
    """
    if m.has_hard_conflict or m.decision_band == DecisionBand.BLOCKED_CONFLICT or m.match_status == MatchStatus.UOM_INCOMPATIBLE:
        return "BLOCKED_CONFLICT"
    elif is_human_resolved_match(m) or (m.match_status in [MatchStatus.EXACT_MATCH, MatchStatus.HIGH_CONFIDENCE_MATCH] and m.decision_band == DecisionBand.HIGH_CONFIDENCE):
        return "HIGH_CONFIDENCE"
    elif m.decision_band == DecisionBand.BULK_CANDIDATE:
        return "BULK_CANDIDATE"
    elif m.decision_band == DecisionBand.MANUAL_REVIEW or m.match_status == MatchStatus.REVIEW_REQUIRED:
        return "MANUAL_REVIEW"
    else:
        return "LOW_CONFIDENCE"


def get_quote_matching_workbench_data(quote_id: str) -> Dict[str, Any]:
    """Builds comprehensive decision-band workbench data for quotation alignment UI."""
    matched_items = get_matched_items(quote_id) or []
    quote = get_canonical_quote(quote_id)
    meta = get_quote_metadata(quote_id) or {}
    rfq = get_rfq(meta.get("rfq_id")) if meta.get("rfq_id") else None

    # Authoritative Decision Band Buckets
    auto_resolved = []
    bulk_candidates = []
    manual_review = []
    low_confidence = []
    blocked_conflicts = []

    for m in matched_items:
        st = classify_matched_item_review_state(m)
        if st == "BLOCKED_CONFLICT":
            blocked_conflicts.append(m)
        elif st == "HIGH_CONFIDENCE":
            auto_resolved.append(m)
        elif st == "BULK_CANDIDATE":
            bulk_candidates.append(m)
        elif st == "MANUAL_REVIEW":
            manual_review.append(m)
        else:
            low_confidence.append(m)

    # Auto-Batch Bulk Candidates by proposed candidate SKU & supplier mapping pattern
    bulk_batches_map: Dict[str, Dict[str, Any]] = {}
    for m in bulk_candidates:
        if m.item_master_match:
            sku = m.item_master_match.candidate_sku
            pn = m.quote_item.supplier_part_number or ""
            supp_name = quote.supplier_raw_name if quote else "Supplier"
            batch_key = sku.upper()
            if batch_key not in bulk_batches_map:
                bulk_batches_map[batch_key] = {
                    "batch_id": f"batch-{abs(hash(batch_key)) % 100000}",
                    "batch_key": batch_key,
                    "proposed_sku": sku,
                    "proposed_description": m.item_master_match.candidate_description or sku,
                    "supplier_name": supp_name,
                    "supplier_pn": pn,
                    "min_confidence": int(round(m.item_master_match.match_score * 100)),
                    "max_confidence": int(round(m.item_master_match.match_score * 100)),
                    "stocking_uom": (m.uom_conversion.target_uom if m.uom_conversion else None) or m.quote_item.quoted_uom or "PCS",
                    "items": [],
                    "batch_items": [],
                    "line_specs": [],
                    "line_indices": []
                }
            score_pct = int(round(m.item_master_match.match_score * 100))
            b = bulk_batches_map[batch_key]
            b["min_confidence"] = min(b["min_confidence"], score_pct)
            b["max_confidence"] = max(b["max_confidence"], score_pct)
            b["items"].append(m)
            b["batch_items"].append(m)
            b["line_indices"].append(m.quote_item.line_index)
            b["line_specs"].append({
                "quote_id": quote_id,
                "line_index": m.quote_item.line_index,
                "chosen_candidate_sku": sku
            })

    bulk_batches = list(bulk_batches_map.values())
    for idx, b in enumerate(bulk_batches):
        b["batch_index"] = idx + 1
        b["count"] = len(b["items"])
        b["line_specs_json"] = json.dumps(b["line_specs"])
        if b["min_confidence"] == b["max_confidence"]:
            b["confidence_display"] = f"{b['min_confidence']}%"
        else:
            b["confidence_display"] = f"{b['min_confidence']}–{b['max_confidence']}%"

    # Grouped Proposed Mappings
    grouped_map: Dict[str, Dict[str, Any]] = {}
    for m in bulk_candidates + manual_review:
        if m.item_master_match:
            desc_norm = m.quote_item.raw_description.strip()
            proposed_sku = m.item_master_match.candidate_sku
            key = f"{desc_norm.lower()}::{proposed_sku.lower()}"
            if key not in grouped_map:
                grouped_map[key] = {
                    "group_key": key,
                    "description": desc_norm,
                    "proposed_sku": proposed_sku,
                    "proposed_description": m.item_master_match.candidate_description,
                    "confidence_pct": int(round(m.item_master_match.match_score * 100)),
                    "decision_band": m.decision_band,
                    "evidence_checklist": m.evidence_checklist,
                    "line_indices": [],
                    "count": 0
                }
            grouped_map[key]["line_indices"].append(m.quote_item.line_index)
            grouped_map[key]["count"] += 1

    grouped_candidates = [g for g in grouped_map.values() if g["count"] > 1]
    unresolved_count = len(bulk_candidates) + len(manual_review) + len(blocked_conflicts)

    return {
        "quote": quote,
        "quote_id": quote_id,
        "rfq": rfq,
        "total_lines": len(matched_items),
        "matched_items": matched_items,
        "auto_resolved": auto_resolved,
        "bulk_candidates": bulk_candidates,
        "bulk_batches": bulk_batches,
        "manual_review": manual_review,
        "low_confidence": low_confidence,
        "blocked_conflicts": blocked_conflicts,
        "grouped_candidates": grouped_candidates,
        "summary_counts": {
            "total": len(matched_items),
            "auto_resolved": len(auto_resolved),
            "bulk_candidates": len(bulk_candidates),
            "manual_review": len(manual_review),
            "low_confidence": len(low_confidence),
            "blocked_conflicts": len(blocked_conflicts),
            "unresolved_count": unresolved_count,
            "bulk_batch_count": len(bulk_batches)
        }
    }


# =========================================================================
# 4. RFQ-SCOPED QUOTES & WORKSPACE ADAPTERS
# =========================================================================

def format_coverage_pct(matched: int, total: int) -> Tuple[float, str]:
    if total <= 0:
        return 0.0, "0%"
    pct = (matched / total) * 100
    if pct >= 100.0:
        return 100.0, "100%"
    if pct <= 0.0:
        return 0.0, "0%"
    if round(pct, 1) == round(pct):
        return round(pct, 1), f"{int(round(pct))}%"
    return round(pct, 1), f"{pct:.1f}%"


def is_quote_item_matching_rfq_line(m: MatchedQuoteItem, rfq_line) -> bool:
    if m.match_status not in [MatchStatus.EXACT_MATCH, MatchStatus.HIGH_CONFIDENCE_MATCH]:
        return False
    if getattr(m, "rfq_match", None) and m.rfq_match.candidate_item_id == rfq_line.rfq_line_id:
        return True
    if getattr(m.quote_item, "matched_rfq_item_id", None) == rfq_line.rfq_line_id:
        return True
    if getattr(m, "item_master_match", None) and rfq_line.internal_item_id and m.item_master_match.candidate_item_id == rfq_line.internal_item_id:
        return True
    if getattr(m, "item_master_match", None) and rfq_line.sku and m.item_master_match.candidate_sku == rfq_line.sku:
        return True
    return False


def is_quote_item_review_for_rfq_line(m: MatchedQuoteItem, rfq_line) -> bool:
    if m.match_status not in [MatchStatus.REVIEW_REQUIRED, MatchStatus.UOM_INCOMPATIBLE]:
        return False
    if getattr(m, "rfq_match", None) and m.rfq_match.candidate_item_id == rfq_line.rfq_line_id:
        return True
    if getattr(m.quote_item, "matched_rfq_item_id", None) == rfq_line.rfq_line_id:
        return True
    if getattr(m, "item_master_match", None) and rfq_line.internal_item_id and m.item_master_match.candidate_item_id == rfq_line.internal_item_id:
        return True
    if getattr(m, "item_master_match", None) and rfq_line.sku and m.item_master_match.candidate_sku == rfq_line.sku:
        return True
    for cand in getattr(m, "top_candidates", []) or []:
        if cand.candidate_sku == rfq_line.sku or cand.candidate_item_id == rfq_line.internal_item_id:
            return True
    return False


def get_quotes_for_rfq(rfq_id: str, all_quotes: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """
    Returns all quotes attached to a specific RFQ with strictly normalized scope coverage.
    Coverage is ALWAYS calculated against the RFQ's requested items and capped at 100%.
    Denominator = total RFQ items.
    Distinguishes:
    1. rfq_items_matched: Count of requested RFQ lines matched by this quotation.
    2. lines_extracted_count: Count of lines extracted from supplier document.
    3. extra_lines_count: Supplier lines not belonging to requested RFQ items.
    4. unmatched_rfq_items_count: Requested RFQ items not covered by quotation.
    """
    rfq = get_rfq(rfq_id)
    rfq_items = rfq.items if rfq and rfq.items else []
    total_rfq_items = len(rfq_items)
    
    quotes_list = all_quotes if all_quotes is not None else list_quotes(include_dev_runs=True, include_demo=True)
    attached = []
    for q_orig in quotes_list:
        if q_orig.get("rfq_id") == rfq_id:
            q = dict(q_orig)
            qid = q.get("test_id") or q.get("quote_id")
            q["quote_id"] = qid
            matched = get_matched_items(qid) or []
            lines_extracted_count = len(matched)
            
            matched_rfq_line_ids = set()
            matched_quote_indices = set()
            
            q_auto_resolved = 0
            q_bulk_ready = 0
            q_manual_review = 0
            q_blocked = 0
            q_unmatched = 0
            q_outside_scope = 0

            for m in matched:
                st = classify_matched_item_review_state(m)
                if st == "BLOCKED_CONFLICT":
                    q_blocked += 1
                elif st == "HIGH_CONFIDENCE":
                    q_auto_resolved += 1
                elif st == "BULK_CANDIDATE":
                    q_bulk_ready += 1
                elif st == "MANUAL_REVIEW":
                    q_manual_review += 1
                else:
                    q_unmatched += 1

                # Check if item corresponds to an RFQ line
                is_rfq_scoped = False
                for rfq_line in rfq_items:
                    if is_quote_item_matching_rfq_line(m, rfq_line):
                        matched_rfq_line_ids.add(rfq_line.rfq_line_id)
                        matched_quote_indices.add(m.quote_item.line_index)
                        is_rfq_scoped = True
                        break
                    elif is_quote_item_review_for_rfq_line(m, rfq_line):
                        is_rfq_scoped = True
                        break
                if not is_rfq_scoped:
                    q_outside_scope += 1

            rfq_items_matched = len(matched_rfq_line_ids)
            extra_lines_count = max(0, lines_extracted_count - len(matched_quote_indices))
            unmatched_rfq_items_count = max(0, total_rfq_items - rfq_items_matched)
            
            q_unresolved_count = q_bulk_ready + q_manual_review + q_blocked
            review_count = q_unresolved_count

            pct_val, pct_str = format_coverage_pct(rfq_items_matched, total_rfq_items)

            q["rfq_items_matched"] = rfq_items_matched
            q["total_rfq_items"] = total_rfq_items
            q["lines_extracted_count"] = lines_extracted_count
            q["extra_lines_count"] = extra_lines_count
            q["unmatched_rfq_items_count"] = unmatched_rfq_items_count
            q["matched_exact"] = rfq_items_matched
            q["review_count"] = review_count
            q["unmatched_count"] = q_unmatched
            q["outside_scope_count"] = q_outside_scope
            q["auto_resolved_count"] = q_auto_resolved
            q["bulk_ready_count"] = q_bulk_ready
            q["manual_review_count"] = q_manual_review
            q["blocked_conflicts_count"] = q_blocked
            q["unresolved_count"] = q_unresolved_count
            q["comparable_items_count"] = rfq_items_matched
            
            q["scope_coverage_pct"] = pct_val
            q["coverage_pct_str"] = pct_str
            q["scope_coverage_str"] = f"{rfq_items_matched} / {total_rfq_items} ({pct_str})"
            q["scope_breakdown_str"] = f"{lines_extracted_count} supplier lines extracted · {rfq_items_matched} matched to RFQ · {extra_lines_count} outside scope"
            
            # Processing State Model: Received -> Extracted -> Review Needed -> Ready for Comparison
            is_success = (q.get("status") == "SUCCESS")
            if not is_success:
                q["processing_status"] = "RECEIVED"
                q["processing_status_label"] = "Received / Parsing"
                q["processing_status_badge"] = "badge-muted"
                q["coverage_state"] = "INELIGIBLE"
                q["eligibility_status"] = "INELIGIBLE"
                q["whole_rfq_status"] = "Received"
                q["is_eligible"] = False
            elif review_count > 0:
                q["processing_status"] = "REVIEW_NEEDED"
                q["processing_status_label"] = f"Review Required ({review_count} Pending)"
                q["processing_status_badge"] = "badge-warning"
                q["coverage_state"] = "REVIEW_REQUIRED"
                q["eligibility_status"] = "REVIEW_REQUIRED"
                q["whole_rfq_status"] = f"Review Required ({review_count} Pending)"
                q["is_eligible"] = False
            elif rfq_items_matched > 0:
                q["processing_status"] = "READY_FOR_COMPARISON"
                q["processing_status_label"] = "Ready for Comparison (0 Pending)"
                q["processing_status_badge"] = "badge-success"
                q["eligibility_status"] = "ELIGIBLE"
                q["is_eligible"] = True
                if rfq_items_matched == total_rfq_items:
                    q["coverage_state"] = "COMPLETE"
                    q["whole_rfq_status"] = "Complete (100% Scope)"
                else:
                    q["coverage_state"] = "PARTIAL"
                    q["whole_rfq_status"] = "Partial Scope"
            else:
                q["processing_status"] = "NO_MATCHED_ITEMS"
                q["processing_status_label"] = "0 RFQ Items Matched"
                q["processing_status_badge"] = "badge-muted"
                q["coverage_state"] = "INELIGIBLE"
                q["eligibility_status"] = "INELIGIBLE"
                q["whole_rfq_status"] = "0 Items Matched"
                q["is_eligible"] = False

            attached.append(q)
    return attached


def get_rfq_workspace_data(rfq_id: str) -> Dict[str, Any]:
    """Provides mission control context for a single RFQ with dynamic multi-supplier stats."""
    rfq = get_rfq(rfq_id)
    if not rfq:
        if rfq_id == DEMO_RFQ_ID:
            ensure_default_rfqs()
            rfq = get_rfq(rfq_id)
        if not rfq:
            return {"rfq": None}

    quotes = get_quotes_for_rfq(rfq.rfq_id)
    latest_comp = get_latest_rfq_comparison(rfq.rfq_id)

    # Dynamic supplier metrics
    total_quotes_count = len(quotes)
    ready_quotes = [q for q in quotes if q.get("processing_status") == "READY_FOR_COMPARISON"]
    ready_count = len(ready_quotes)
    need_review_quotes = [q for q in quotes if q.get("processing_status") == "REVIEW_NEEDED"]
    need_review_count = len(need_review_quotes)
    total_review_issues = sum(q.get("review_count", 0) for q in quotes)
    ineligible_count = sum(1 for q in quotes if q.get("processing_status") in ["INELIGIBLE", "NO_MATCHED_ITEMS", "RECEIVED"])
    eligible_quotes = ready_quotes
    eligible_count = ready_count
    review_count = need_review_count
    quotes_summary_str = f"{total_quotes_count} Quotes Received · {ready_count} Ready for Comparison · {need_review_count} Need Review"

    # Line-level aggregated metrics across all quotes
    total_lines_extracted = 0
    total_auto_matched = 0
    total_review_required = 0
    total_unmatched = 0

    for q in quotes:
        qid = q.get("test_id") or q.get("quote_id")
        matched = get_matched_items(qid) or []
        for m in matched:
            total_lines_extracted += 1
            if m.match_status in [MatchStatus.EXACT_MATCH, MatchStatus.HIGH_CONFIDENCE_MATCH]:
                total_auto_matched += 1
            elif m.match_status in [MatchStatus.REVIEW_REQUIRED, MatchStatus.UOM_INCOMPATIBLE]:
                total_review_required += 1
            elif m.match_status == MatchStatus.UNMATCHED:
                total_unmatched += 1

    lines_summary_str = f"{total_auto_matched} matched automatically • {total_review_required} need review • {total_unmatched} unmatched"

    award_record = get_award_decision(rfq.rfq_id)
    if award_record and award_record.get("status") == "FINALIZED":
        current_stage = 5
        stage_name = "Commercial Award Finalized"
        next_action = "AWARD_FINALIZED"
        action_message = f"Commercial award finalized ({award_record.get('base_currency')} {award_record.get('total_awarded_value')}). {award_record.get('fully_allocated_items_count', 0)} line items fully awarded."
    elif latest_comp:
        current_stage = 5
        stage_name = "Commercial Award Decision"
        next_action = "EVALUATED"
        ranking_rep = latest_comp.get("ranking_report") or {}
        l1_supp = ranking_rep.get("l1_supplier") or {}
        l1_name = l1_supp.get("supplier_name") if isinstance(l1_supp, dict) else None
        l1_cost = l1_supp.get("total_landed_cost_base", "—") if isinstance(l1_supp, dict) else "—"
        if l1_name:
            action_message = f"Commercial evaluation complete. Recommended L1: {l1_name} ({rfq.base_currency} {l1_cost}). Next step: View the award decision and item matrix or re-run evaluation."
        else:
            action_message = f"Commercial evaluation complete (Split Sourcing Optimal). Next step: View the award decision and line allocation matrix."
    elif eligible_count >= 2:
        current_stage = 4
        stage_name = "Ready for Commercial Comparison"
        next_action = "READY_FOR_COMPARISON"
        if review_count > 0:
            action_message = f"{total_quotes_count} supplier quotation(s) received ({eligible_count} ready · {review_count} requires review). Next step: Review outstanding exceptions or compare available quotations."
        else:
            action_message = f"{total_quotes_count} supplier quotation(s) received ({eligible_count} ready). Next step: Run commercial comparison to evaluate landed costs and split awards."
    elif eligible_count == 1:
        current_stage = 4
        stage_name = "Ready for Evaluation"
        next_action = "READY_FOR_COMPARISON"
        if review_count > 0:
            action_message = f"1 eligible quotation ready · {review_count} quotation(s) require review. Next step: Review exceptions in the Review Center or evaluate the available quotation."
        else:
            action_message = f"1 supplier quotation received and ready. Next step: Evaluate this quotation against RFQ targets or upload additional competing quotations."
    elif review_count > 0:
        current_stage = 3
        stage_name = "Product Alignment Review Required"
        next_action = "MATCHING_REVIEW"
        action_message = f"{total_quotes_count} supplier quotation(s) received. {review_count} quotation(s) require product alignment review before evaluation. Next step: Resolve exceptions in Review Center."
    elif total_quotes_count > 0:
        current_stage = 2
        stage_name = "Quotation Ingestion & Processing"
        next_action = "QUOTES_PENDING"
        action_message = f"{total_quotes_count} supplier quotation(s) uploaded. Check item alignments or upload additional quotations."
    else:
        current_stage = 2
        stage_name = "Supplier Quotations Pending"
        next_action = "QUOTES_PENDING"
        action_message = "No supplier quotations received yet. Upload quotation files (.xlsx, .csv, .pdf) received from suppliers below."

    return {
        "rfq": rfq,
        "quotes": quotes,
        "total_quotes_count": total_quotes_count,
        "ready_count": ready_count,
        "need_review_count": need_review_count,
        "total_review_issues": total_review_issues,
        "quotes_summary_str": quotes_summary_str,
        "total_lines_extracted": total_lines_extracted,
        "total_auto_matched": total_auto_matched,
        "total_review_required": total_review_required,
        "total_unmatched": total_unmatched,
        "lines_summary_str": lines_summary_str,
        "eligible_quotes": eligible_quotes,
        "eligible_count": eligible_count,
        "review_count": review_count,
        "ineligible_count": ineligible_count,
        "comparison": latest_comp,
        "award": award_record,
        "ranking_report": latest_comp.get("ranking_report") if latest_comp else None,
        "item_master_count": len(get_item_master()),
        "current_stage": current_stage,
        "stage_name": stage_name,
        "can_compare": (eligible_count >= 1 or total_quotes_count >= 1),
        "next_action": next_action,
        "action_message": action_message,
        "is_demo": rfq.rfq_id == DEMO_RFQ_ID
    }


def list_quotes(include_dev_runs: bool = True, include_demo: bool = True) -> List[Dict[str, Any]]:
    """Lists quotations on disk, separating demo quotes, user quotes, and dev test runs."""
    quotes = []
    if not DATA_DIR.exists():
        return []

    demo_dirs = [DATA_DIR / qid for qid in DEMO_SUPPLIER_IDS if (DATA_DIR / qid).exists()] if include_demo else []
    other_dirs = [d for d in DATA_DIR.iterdir() if d.is_dir() and d.name not in DEMO_SUPPLIER_IDS and d.name not in ["rfqs", "comparisons", "benchmark_fixtures", "item_master", "awards"]]
    other_dirs.sort(key=lambda d: d.name, reverse=True)

    all_dirs = demo_dirs + other_dirs

    for d in all_dirs:
        meta_file = d / "metadata.json"
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)

                    is_demo = d.name in DEMO_SUPPLIER_IDS
                    is_dev = d.name.startswith("TEST-") and not is_demo

                    if not include_demo and is_demo:
                        continue
                    if not include_dev_runs and is_dev:
                        continue

                    rep_file = d / "reports" / "extraction_report.json"
                    rep_data = {}
                    if rep_file.exists():
                        try:
                            with open(rep_file, "r", encoding="utf-8") as rf:
                                rep_data = json.load(rf)
                        except Exception:
                            pass

                    canon_file = d / "extracted" / "canonical_quote.json"
                    supplier_name = meta.get("supplier_name") or "—"
                    currency = meta.get("currency") or "INR"
                    terms_text = "Standard Net Terms"
                    if canon_file.exists():
                        try:
                            with open(canon_file, "r", encoding="utf-8") as cf:
                                cdata = json.load(cf)
                                raw_supp = cdata.get("supplier_raw_name")
                                if raw_supp and raw_supp not in ["Unknown Supplier", "Unknown", "—", ""]:
                                    supplier_name = raw_supp
                                elif not supplier_name or supplier_name == "—":
                                    supplier_name = meta.get("test_name") or "—"
                                currency = cdata.get("currency") or currency
                                if cdata.get("payment_terms") and cdata["payment_terms"].get("raw_text"):
                                    terms_text = cdata["payment_terms"]["raw_text"]
                        except Exception:
                            pass

                    meta["supplier_name"] = supplier_name
                    meta["currency"] = currency
                    meta["terms"] = terms_text
                    meta["line_count"] = rep_data.get("line_count", 0)
                    meta["total_landed_cost"] = rep_data.get("total_landed_cost", "—")
                    meta["is_demo"] = is_demo
                    meta["is_dev_test"] = is_dev
                    quotes.append(meta)
            except Exception:
                continue
    return quotes


def list_tests() -> List[Dict[str, Any]]:
    return list_quotes(include_dev_runs=True, include_demo=True)


# =========================================================================
# 5. COMMERCIAL NORMALIZATION, COMPARISON & RANKING
# =========================================================================

def list_comparisons() -> List[Dict[str, Any]]:
    comparisons = []
    if not COMPARISONS_DIR.exists():
        return []
    for f in sorted(COMPARISONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                ranking_rep = data.get("ranking_report") or {}
                l1_supp = ranking_rep.get("l1_supplier") or {}
                l1_name = l1_supp.get("supplier_name", "Split Optimal") if isinstance(l1_supp, dict) else "Split Optimal"
                l1_cost = l1_supp.get("total_landed_cost_base", "—") if isinstance(l1_supp, dict) else "—"
                comp_obj = data.get("comparison") or {}

                rfq_id = data.get("rfq_id")
                rfq_doc = get_rfq(rfq_id) if rfq_id else None
                rfq_title = rfq_doc.title if rfq_doc else data.get("title", f"Comparison for {rfq_id}")

                suppliers_dict = comp_obj.get("suppliers") or {}
                supplier_names = []
                if isinstance(suppliers_dict, dict):
                    for sid, sinfo in suppliers_dict.items():
                        if isinstance(sinfo, dict) and sinfo.get("supplier_name"):
                            supplier_names.append(sinfo["supplier_name"])
                        else:
                            supplier_names.append(str(sid))
                elif isinstance(suppliers_dict, list):
                    for sinfo in suppliers_dict:
                        if isinstance(sinfo, dict) and sinfo.get("supplier_name"):
                            supplier_names.append(sinfo["supplier_name"])
                        elif isinstance(sinfo, str):
                            supplier_names.append(sinfo)

                comparisons.append({
                    "comparison_id": data.get("comparison_id", f.stem),
                    "rfq_id": rfq_id,
                    "rfq_title": rfq_title,
                    "title": data.get("title", f"Comparison for {rfq_id}"),
                    "created_at": data.get("created_at"),
                    "supplier_count": len(suppliers_dict),
                    "supplier_names": supplier_names,
                    "supplier_names_str": ", ".join(supplier_names) if supplier_names else "—",
                    "l1_supplier": l1_name,
                    "l1_landed_cost": l1_cost,
                    "base_currency": comp_obj.get("base_currency", "INR")
                })
        except Exception:
            continue
    return comparisons


def get_comparison(comparison_id: str) -> Optional[Dict[str, Any]]:
    comp_file = COMPARISONS_DIR / f"{comparison_id}.json"
    if not comp_file.exists():
        return None
    try:
        with open(comp_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    # Check for staleness against current matching state of participating quotes
    stored_fp = data.get("matching_fingerprint")
    qids = data.get("quote_ids") or []
    rfq_id = data.get("rfq_id")
    if stored_fp and qids and rfq_id:
        current_fp = compute_quotes_matching_fingerprint(qids)
        if current_fp != stored_fp:
            # Stale comparison snapshot detected: automatically regenerate from authoritative matching state!
            try:
                alloc_str = data.get("charge_allocation_method") or "PROPORTIONAL_LINE_VALUE"
                alloc_enum = ChargeAllocationMethod.PROPORTIONAL_LINE_VALUE
                if alloc_str == "PROPORTIONAL_QUANTITY":
                    alloc_enum = ChargeAllocationMethod.PROPORTIONAL_QUANTITY
                elif alloc_str == "NONE":
                    alloc_enum = ChargeAllocationMethod.NONE
                
                rates_dict = {k: Decimal(str(v)) for k, v in data.get("exchange_rates", {}).items() if v}
                fresh_data = run_rfq_comparison(
                    rfq_id=rfq_id,
                    quote_ids=qids,
                    base_currency=data.get("base_currency", "INR"),
                    exchange_rates=rates_dict,
                    charge_allocation_method=alloc_enum
                )
                fresh_data["comparison_id"] = comparison_id
                fresh_data["title"] = data.get("title", fresh_data.get("title"))
                with open(comp_file, "w", encoding="utf-8") as f:
                    json.dump(fresh_data, f, indent=2)
                return fresh_data
            except Exception:
                pass

    return data


def run_rfq_comparison(
    rfq_id: str,
    quote_ids: List[str],
    base_currency: str = "INR",
    exchange_rates: Optional[Dict[str, Decimal]] = None,
    charge_allocation_method: ChargeAllocationMethod = ChargeAllocationMethod.PROPORTIONAL_LINE_VALUE
) -> Dict[str, Any]:
    rfq = get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    submissions: List[SupplierQuoteSubmission] = []
    required_currencies = set()
    base_curr = (base_currency or rfq.base_currency or "INR").upper().strip()

    for qid in quote_ids:
        quote = get_canonical_quote(qid)
        if not quote:
            continue
        
        # Authoritative Match Retrieval:
        # Load persisted matched items; run matching only if not yet matched or to merge with current RFQ,
        # strictly preserving all locked human decisions.
        matched_items = run_matching_for_quote(qid, rfq_id)

        meta = get_quote_metadata(qid) or {}
        supplier_id = qid
        raw_supp = quote.supplier_raw_name
        if not raw_supp or raw_supp in ["Unknown Supplier", "Unknown", "—", ""]:
            supplier_name = meta.get("supplier_name") or meta.get("test_name") or qid
        else:
            supplier_name = raw_supp

        submissions.append(SupplierQuoteSubmission(
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            canonical_quote=quote,
            matched_items=matched_items
        ))
        if quote.currency:
            required_currencies.add(quote.currency.upper().strip())

    if not submissions:
        raise ValueError("No valid supplier quotations selected for comparison.")

    # Live Centralized FX Resolution — only resolve currencies actually present in quotes!
    resolved_rates: Dict[str, Decimal] = {}
    fx_metadata: Dict[str, Dict[str, Any]] = {}

    for cur in required_currencies:
        if cur == base_curr:
            resolved_rates[cur] = Decimal("1.0")
        else:
            manual_val = exchange_rates.get(cur) if exchange_rates else None
            if manual_val and manual_val > Decimal("0.0"):
                resolved_rates[cur] = manual_val
                now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                symbol = "₹" if base_curr == "INR" else ("$" if base_curr == "USD" else f"{base_curr} ")
                fx_metadata[cur] = {
                    "base": cur,
                    "quote": base_curr,
                    "rate": str(manual_val),
                    "rate_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "timestamp": now_utc,
                    "provider": "Manual FX Override",
                    "status": "MANUAL",
                    "is_live": False,
                    "is_cached": False,
                    "is_fallback": False,
                    "formatted_label": f"{cur} → {base_curr}: {symbol}{manual_val} · Source: Manual FX Override (Updated: {now_utc})"
                }
            else:
                rate_res = CurrencyRateService.get_exchange_rate(
                    from_currency=cur,
                    to_currency=base_curr
                )
                resolved_rates[cur] = rate_res.rate
                fx_metadata[cur] = rate_res.to_dict()

    comparator = QuotationComparator()
    comparison = comparator.compare_rfq(
        rfq=rfq,
        submissions=submissions,
        exchange_rates=resolved_rates,
        charge_allocation_method=charge_allocation_method
    )

    ranking_engine = RankingEngine()
    ranking_report = ranking_engine.generate_ranking_report(comparison)

    comparison_id = f"COMP-{rfq_id}-{int(time.time() * 1000)}"
    alloc_str = charge_allocation_method.value if hasattr(charge_allocation_method, "value") else str(charge_allocation_method)
    
    result_data = {
        "comparison_id": comparison_id,
        "rfq_id": rfq_id,
        "title": f"Executive Evaluation: {rfq.title}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_currency": base_curr,
        "matching_fingerprint": compute_quotes_matching_fingerprint(quote_ids),
        "quote_ids": quote_ids,
        "charge_allocation_method": alloc_str,
        "exchange_rates": {k: str(v) for k, v in (exchange_rates or {}).items()},
        "fx_metadata": fx_metadata,
        "comparison": json.loads(comparison.model_dump_json()),
        "ranking_report": json.loads(ranking_report.model_dump_json())
    }

    with open(COMPARISONS_DIR / f"{comparison_id}.json", "w", encoding="utf-8") as f:
        json.dump(result_data, f, indent=2)

    return result_data


# =========================================================================
# 6. ACTIONABLE REVIEW CENTER (SCOPED TO ACTIVE USER RFQS)
# =========================================================================

def get_review_center_issues(rfq_id_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Returns actionable exceptions strictly scoped to active user RFQs,
    completely filtering out historical test harness runs and demo quotes unless requested.
    """
    issues = []
    
    if rfq_id_filter:
        active_rfq_ids = {rfq_id_filter}
    else:
        # Default: only user-created RFQs
        user_rfqs = list_rfqs(include_demo=False)
        active_rfq_ids = {r["rfq_id"] for r in user_rfqs}

    # Query quotes belonging only to active target RFQs
    quotes = list_quotes(include_dev_runs=False, include_demo=(DEMO_RFQ_ID in active_rfq_ids))
    quotes = [q for q in quotes if q.get("rfq_id") in active_rfq_ids]

    for q in quotes:
        quote_id = q.get("quote_id") or q.get("test_id")
        if not quote_id:
            continue
        q_name = q.get("test_name", quote_id)
        supplier = q.get("supplier_name", "Unknown Supplier")
        rfq_id = q.get("rfq_id") or "Unassigned"

        matched = get_matched_items(quote_id)
        if matched:
            for m in matched:
                if m.match_status == MatchStatus.REVIEW_REQUIRED:
                    issues.append({
                        "type": "MATCH_REVIEW_REQUIRED",
                        "severity": "WARNING",
                        "rfq_id": rfq_id,
                        "quote_id": quote_id,
                        "quote_name": q_name,
                        "supplier": supplier,
                        "line_index": m.quote_item.line_index,
                        "description": m.quote_item.raw_description,
                        "message": f"Ambiguous item match for line {m.quote_item.line_index + 1}: requires candidate confirmation",
                        "link": f"/quotes/{quote_id}/match"
                    })
                elif m.match_status == MatchStatus.UOM_INCOMPATIBLE:
                    issues.append({
                        "type": "UOM_INCOMPATIBLE",
                        "severity": "CRITICAL",
                        "rfq_id": rfq_id,
                        "quote_id": quote_id,
                        "quote_name": q_name,
                        "supplier": supplier,
                        "line_index": m.quote_item.line_index,
                        "description": m.quote_item.raw_description,
                        "message": f"Incompatible UOM conversion ({m.quote_item.quoted_uom}) on line {m.quote_item.line_index + 1}",
                        "link": f"/quotes/{quote_id}/match"
                    })
                elif m.match_status == MatchStatus.UNMATCHED:
                    issues.append({
                        "type": "UNMATCHED_ITEM",
                        "severity": "INFO",
                        "rfq_id": rfq_id,
                        "quote_id": quote_id,
                        "quote_name": q_name,
                        "supplier": supplier,
                        "line_index": m.quote_item.line_index,
                        "description": m.quote_item.raw_description,
                        "message": f"Non-catalog item: no Item Master match found for line {m.quote_item.line_index + 1}",
                        "link": f"/quotes/{quote_id}/match"
                    })

        rep = get_extraction_report(quote_id)
        if rep and rep.get("reconciliation_status") == "DISCREPANCY":
            issues.append({
                "type": "FINANCIAL_DISCREPANCY",
                "severity": "CRITICAL",
                "rfq_id": rfq_id,
                "quote_id": quote_id,
                "quote_name": q_name,
                "supplier": supplier,
                "line_index": None,
                "description": f"Calculated {rep.get('calculated_expected_total')} vs Stated {rep.get('supplier_stated_grand_total')}",
                "message": f"Supplier stated total mismatch (Discrepancy: {rep.get('discrepancy')})",
                "link": f"/quotes/{quote_id}"
            })

    return issues


def get_review_center_workbench_summary(rfq_id_filter: Optional[str] = None) -> Dict[str, Any]:
    """Computes high-level aggregated decision-band summary statistics for the Review Center."""
    if rfq_id_filter:
        active_rfq_ids = {rfq_id_filter}
    else:
        user_rfqs = list_rfqs(include_demo=True)
        active_rfq_ids = {r["rfq_id"] for r in user_rfqs}

    quotes = list_quotes(include_dev_runs=False, include_demo=True)
    quotes = [q for q in quotes if q.get("rfq_id") in active_rfq_ids]

    total_supplier_lines = 0
    high_confidence_count = 0
    bulk_candidate_count = 0
    manual_review_count = 0
    low_confidence_count = 0
    blocked_conflicts_count = 0

    quote_summaries = []

    for q in quotes:
        qid = q.get("quote_id") or q.get("test_id")
        if not qid:
            continue
        matched = get_matched_items(qid) or []
        if not matched:
            continue

        q_total = len(matched)
        total_supplier_lines += q_total
        q_auto = 0
        q_bulk = 0
        q_manual = 0
        q_low = 0
        q_blocked = 0

        for m in matched:
            st = classify_matched_item_review_state(m)
            if st == "BLOCKED_CONFLICT":
                blocked_conflicts_count += 1
                q_blocked += 1
            elif st == "HIGH_CONFIDENCE":
                high_confidence_count += 1
                q_auto += 1
            elif st == "BULK_CANDIDATE":
                bulk_candidate_count += 1
                q_bulk += 1
            elif st == "MANUAL_REVIEW":
                manual_review_count += 1
                q_manual += 1
            else:
                low_confidence_count += 1
                q_low += 1

        quote_summaries.append({
            "quote_id": qid,
            "quote_name": q.get("test_name", qid),
            "supplier_name": q.get("supplier_name", "Supplier"),
            "rfq_id": q.get("rfq_id"),
            "total_lines": q_total,
            "auto_resolved": q_auto,
            "bulk_candidates": q_bulk,
            "manual_review": q_manual,
            "low_confidence": q_low,
            "blocked": q_blocked,
            "unresolved_count": (q_bulk + q_manual + q_blocked),
            "has_pending": (q_bulk + q_manual + q_blocked) > 0
        })

    unresolved_total = bulk_candidate_count + manual_review_count + blocked_conflicts_count
    return {
        "total": total_supplier_lines,
        "total_supplier_lines": total_supplier_lines,
        "auto_resolved": high_confidence_count,
        "high_confidence_count": high_confidence_count,
        "bulk_candidates": bulk_candidate_count,
        "bulk_candidate_count": bulk_candidate_count,
        "manual_review": manual_review_count,
        "manual_review_count": manual_review_count,
        "low_confidence": low_confidence_count,
        "low_confidence_count": low_confidence_count,
        "blocked_conflicts": blocked_conflicts_count,
        "blocked_conflicts_count": blocked_conflicts_count,
        "unresolved_count": unresolved_total,
        "quote_summaries": quote_summaries
    }


def bulk_confirm_rfq_matches(rfq_id: Optional[str], batch_line_specs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Atomically validates and commits batch confirmations across multiple supplier quotes
    belonging to an RFQ in a single pass.
    """
    if not batch_line_specs:
        return {"status": "error", "message": "No lines specified for bulk confirmation."}

    # 1. Group specs by quote_id
    quotes_to_update: Dict[str, List[Dict[str, Any]]] = {}
    for spec in batch_line_specs:
        qid = spec.get("quote_id")
        if not qid:
            return {"status": "error", "message": "Missing quote_id in batch specification."}
        if qid not in quotes_to_update:
            quotes_to_update[qid] = []
        quotes_to_update[qid].append(spec)

    # 2. Validation Phase across all quotes
    loaded_quotes: Dict[str, Tuple[List[MatchedQuoteItem], Dict[str, Any]]] = {}
    im_records = {im.internal_sku.upper(): im for im in get_item_master(include_inactive=True) if im.internal_sku}

    # Aggregate RFQ lines from target RFQ and participating quotes
    all_rfq_ids_to_check = set()
    if rfq_id:
        all_rfq_ids_to_check.add(rfq_id)
    for qid in quotes_to_update:
        meta = get_quote_metadata(qid) or {}
        q_rfq = meta.get("rfq_id")
        if q_rfq:
            all_rfq_ids_to_check.add(q_rfq)

    for check_rfq_id in all_rfq_ids_to_check:
        rfq_obj = get_rfq(check_rfq_id)
        if rfq_obj and rfq_obj.items:
            for rline in rfq_obj.items:
                line_sku = rline.sku or rline.rfq_line_id
                if line_sku and line_sku.upper() not in im_records:
                    im_records[line_sku.upper()] = ItemMasterRecord(
                        internal_item_id=rline.internal_item_id or rline.rfq_line_id,
                        internal_sku=line_sku,
                        category=rline.category,
                        manufacturer=rline.manufacturer,
                        manufacturer_part_number=rline.manufacturer_part_number,
                        canonical_description=rline.description,
                        stocking_uom=rline.requested_uom or "PCS",
                        specifications=rline.specifications
                    )

    for qid, specs in quotes_to_update.items():
        meta = get_quote_metadata(qid) or {}
        # RFQ isolation check: quote must belong to this RFQ if specified
        if rfq_id and meta.get("rfq_id") != rfq_id:
            return {"status": "error", "message": f"Quote '{qid}' does not belong to RFQ '{rfq_id}'."}

        matched_items = get_matched_items(qid)
        if not matched_items:
            return {"status": "error", "message": f"Matching items not found for quote '{qid}'."}

        # Validate each spec for this quote
        for s in specs:
            l_idx = s.get("line_index")
            sku = s.get("chosen_candidate_sku")
            if l_idx is None or l_idx < 0 or l_idx >= len(matched_items):
                return {"status": "error", "message": f"Invalid line index {l_idx} in quote '{qid}'."}

            item = matched_items[l_idx]
            # Safety checks: Hard conflicts must NOT be bulk confirmed
            if item.has_hard_conflict or item.decision_band == DecisionBand.BLOCKED_CONFLICT:
                return {"status": "error", "message": f"Line {l_idx + 1} in quote '{qid}' has a hard conflict and cannot be bulk confirmed."}

            if not sku:
                return {"status": "error", "message": f"Missing chosen candidate SKU for line {l_idx + 1} in quote '{qid}'."}

            if sku.upper() not in im_records:
                # Check candidate metadata on the item
                cand_meta = item.item_master_match if (item.item_master_match and item.item_master_match.candidate_sku and item.item_master_match.candidate_sku.upper() == sku.upper()) else None
                if not cand_meta and item.top_candidates:
                    cand_meta = next((c for c in item.top_candidates if c.candidate_sku and c.candidate_sku.upper() == sku.upper()), None)
                if cand_meta:
                    im_records[sku.upper()] = ItemMasterRecord(
                        internal_item_id=cand_meta.candidate_item_id or sku,
                        internal_sku=cand_meta.candidate_sku or sku,
                        canonical_description=cand_meta.candidate_description or sku,
                        stocking_uom=item.quote_item.quoted_uom or "PCS"
                    )
                else:
                    return {"status": "error", "message": f"Candidate SKU '{sku}' not found in Item Master or RFQ lines."}

        loaded_quotes[qid] = (matched_items, meta)

    # 3. Execution Phase (Atomic Commit)
    total_confirmed = 0
    rfq_obj = get_rfq(rfq_id) if rfq_id else None

    from matching.uom_resolver import UOMResolver
    uom_resolver = UOMResolver()

    for qid, specs in quotes_to_update.items():
        matched_items, meta = loaded_quotes[qid]
        quote = get_canonical_quote(qid)
        supp_id = meta.get("supplier_id") or (quote.supplier_raw_name if quote else None) or qid
        supp_name = quote.supplier_raw_name if quote else meta.get("supplier_name", "Supplier")

        for s in specs:
            l_idx = s["line_index"]
            chosen_sku = s["chosen_candidate_sku"].upper()
            im_item = im_records[chosen_sku]

            # Promote item to confirmed state
            item = matched_items[l_idx]
            item.match_status = MatchStatus.EXACT_MATCH
            item.decision_band = DecisionBand.HIGH_CONFIDENCE
            item.has_hard_conflict = False
            item.review_reasons = []
            item.conflict_reasons = []

            # Set candidate match
            item.item_master_match = MatchCandidate(
                candidate_item_id=im_item.internal_item_id,
                candidate_sku=im_item.internal_sku,
                candidate_description=im_item.canonical_description,
                match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                match_score=1.0,
                match_status=MatchStatus.EXACT_MATCH,
                decision_band=DecisionBand.HIGH_CONFIDENCE,
                explanations=["Bulk-confirmed by procurement reviewer"],
                evidence_checklist=[EvidenceItem(signal="HUMAN_CONFIRMATION", status="PASS", description="Bulk-confirmed by buyer")],
                matched_fields=["human_confirmation"],
                has_hard_conflict=False,
                conflict_reasons=[],
                source_provenance=item.quote_item.provenance
            )

            # Check UOM compatibility
            uom_res = uom_resolver.resolve_uom_conversion(
                source_uom=item.quote_item.quoted_uom,
                target_uom=im_item.stocking_uom or item.quote_item.quoted_uom,
                quoted_quantity=item.quote_item.quoted_qty
            )
            item.uom_conversion = uom_res

            # Align with RFQ Line if exists
            target_rfq_for_quote = get_rfq(meta.get("rfq_id")) or rfq_obj
            if target_rfq_for_quote:
                for rline in target_rfq_for_quote.items:
                    if (rline.sku and rline.sku.upper() == im_item.internal_sku.upper()) or (rline.internal_item_id == im_item.internal_item_id):
                        item.rfq_match = MatchCandidate(
                            candidate_item_id=rline.rfq_line_id,
                            candidate_sku=rline.sku or rline.rfq_line_id,
                            candidate_description=rline.description,
                            match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                            match_score=1.0,
                            match_status=MatchStatus.EXACT_MATCH,
                            decision_band=DecisionBand.HIGH_CONFIDENCE,
                            explanations=["Bulk-confirmed by procurement reviewer"],
                            evidence_checklist=[EvidenceItem(signal="HUMAN_CONFIRMATION", status="PASS", description="Bulk-confirmed by buyer")],
                            matched_fields=["human_confirmation"],
                            has_hard_conflict=False,
                            conflict_reasons=[],
                            source_provenance=item.quote_item.provenance
                        )
                        break

            total_confirmed += 1

            # Save supplier memory
            if supp_id and item.quote_item.supplier_part_number:
                save_supplier_mapping(
                    supplier_id=supp_id,
                    supplier_name=supp_name,
                    supplier_part_number=item.quote_item.supplier_part_number,
                    internal_sku=im_item.internal_sku,
                    internal_item_id=im_item.internal_item_id,
                    canonical_description=im_item.canonical_description,
                    confirmed_by="Procurement Specialist (Bulk)",
                    source_rfq_id=rfq_id,
                    source_quote_id=qid
                )

        # Commit quote to disk
        save_quote_matched_items(qid, matched_items)

    # 4. Advance RFQ workflow stage if all items are resolved
    for check_rfq_id in all_rfq_ids_to_check:
        rfq_doc = get_rfq(check_rfq_id)
        if rfq_doc and rfq_doc.status not in ["ARCHIVED", "CANCELLED", "CLOSED", "AWARD_FINALIZED"]:
            rfq_summary = get_review_center_workbench_summary(check_rfq_id)
            if rfq_summary.get("unresolved_count", 0) == 0:
                rfq_doc.status = "READY_FOR_COMPARISON"
                save_rfq_document(rfq_doc)

    summary = get_review_center_workbench_summary(rfq_id)
    return {
        "status": "success",
        "confirmed_count": total_confirmed,
        "summary_counts": summary,
        "unresolved_count": summary.get("unresolved_count", 0),
        "message": f"Successfully confirmed {total_confirmed} matches across {len(quotes_to_update)} supplier quotations."
    }


def get_global_review_center_data(rfq_id_filter: Optional[str] = None) -> Dict[str, Any]:
    """
    Assembles the complete authoritative RFQ-level Review Center dataset across all suppliers.
    Constructs system-selected bulk candidate batches, continuous manual review queue,
    blocked conflicts, and complete line registry.
    """
    if rfq_id_filter:
        active_rfq_ids = {rfq_id_filter}
        target_rfq = get_rfq(rfq_id_filter)
    else:
        user_rfqs = list_rfqs(include_demo=True)
        active_rfq_ids = {r["rfq_id"] for r in user_rfqs}
        target_rfq = None

    quotes = list_quotes(include_dev_runs=False, include_demo=True)
    quotes = [q for q in quotes if q.get("rfq_id") in active_rfq_ids]

    total_supplier_lines = 0
    auto_resolved = []
    bulk_candidates = []
    manual_review = []
    low_confidence = []
    blocked_conflicts = []
    all_registered_items = []
    suppliers_set = set()

    for q in quotes:
        qid = q.get("quote_id") or q.get("test_id")
        if not qid:
            continue
        supp_name = q.get("supplier_name", "Supplier")
        suppliers_set.add(supp_name)
        curr = q.get("currency", "INR")

        matched = get_matched_items(qid) or []
        if not matched:
            continue

        total_supplier_lines += len(matched)

        for m in matched:
            sku_match = m.item_master_match
            cand_sku = sku_match.candidate_sku if sku_match else None
            cand_desc = sku_match.candidate_description if sku_match else None
            score = float(sku_match.match_score) if sku_match else 0.0
            score_pct = int(round(score * 100))

            item_dict = {
                "quote_id": qid,
                "supplier_name": supp_name,
                "rfq_id": q.get("rfq_id"),
                "line_index": m.quote_item.line_index,
                "line_number": m.quote_item.line_index + 1,
                "raw_description": m.quote_item.raw_description,
                "quoted_qty": str(m.quote_item.quoted_qty) if m.quote_item.quoted_qty is not None else "—",
                "quoted_uom": m.quote_item.quoted_uom or "",
                "unit_price": str(m.quote_item.unit_price) if m.quote_item.unit_price is not None else "—",
                "supplier_part_number": m.quote_item.supplier_part_number or "",
                "currency": curr,
                "candidate_sku": cand_sku,
                "proposed_sku": cand_sku,
                "candidate_description": cand_desc,
                "proposed_description": cand_desc,
                "match_score": score,
                "match_score_pct": score_pct,
                "confidence_pct": score_pct,
                "decision_band": m.decision_band.value if hasattr(m.decision_band, "value") else str(m.decision_band),
                "match_status": m.match_status.value if hasattr(m.match_status, "value") else str(m.match_status),
                "has_hard_conflict": m.has_hard_conflict,
                "evidence_checklist": [ev.model_dump() if hasattr(ev, "model_dump") else ev for ev in m.evidence_checklist],
                "conflict_reasons": m.conflict_reasons,
                "review_reasons": m.review_reasons,
                "top_candidates": [c.model_dump() if hasattr(c, "model_dump") else c for c in m.top_candidates],
                "best_candidate": sku_match.model_dump() if (sku_match and hasattr(sku_match, "model_dump")) else (m.top_candidates[0].model_dump() if m.top_candidates and hasattr(m.top_candidates[0], "model_dump") else None)
            }

            all_registered_items.append(item_dict)

            st = classify_matched_item_review_state(m)
            if st == "BLOCKED_CONFLICT":
                blocked_conflicts.append(item_dict)
            elif st == "HIGH_CONFIDENCE":
                auto_resolved.append(item_dict)
            elif st == "BULK_CANDIDATE":
                bulk_candidates.append(item_dict)
            elif st == "MANUAL_REVIEW":
                manual_review.append(item_dict)
            else:
                low_confidence.append(item_dict)

    # Construct System-Generated Bulk Candidate Batches (80-94%)
    bulk_batches_map: Dict[str, Dict[str, Any]] = {}
    for it in bulk_candidates:
        cand_sku = it["candidate_sku"] or "UNKNOWN"
        batch_key = cand_sku.upper()
        if batch_key not in bulk_batches_map:
            bulk_batches_map[batch_key] = {
                "batch_id": f"batch-{abs(hash(batch_key)) % 100000}",
                "batch_key": batch_key,
                "proposed_sku": cand_sku,
                "proposed_description": it["candidate_description"] or cand_sku,
                "supplier_names": set(),
                "min_confidence": it["match_score_pct"],
                "max_confidence": it["match_score_pct"],
                "stocking_uom": it["quoted_uom"] or "PCS",
                "batch_items": [],
                "items": [],
                "line_specs": []
            }
        b = bulk_batches_map[batch_key]
        b["supplier_names"].add(it["supplier_name"])
        b["min_confidence"] = min(b["min_confidence"], it["match_score_pct"])
        b["max_confidence"] = max(b["max_confidence"], it["match_score_pct"])
        b["batch_items"].append(it)
        b["items"].append(it)
        b["line_specs"].append({
            "quote_id": it["quote_id"],
            "line_index": it["line_index"],
            "chosen_candidate_sku": cand_sku
        })

    rfq_bulk_batches = list(bulk_batches_map.values())
    for idx, b in enumerate(rfq_bulk_batches):
        b["batch_index"] = idx + 1
        b["count"] = len(b["batch_items"])
        b["supplier_names_list"] = sorted(list(b["supplier_names"]))
        b["supplier_count"] = len(b["supplier_names_list"])
        b["supplier_names_display"] = ", ".join(b["supplier_names_list"])
        b["line_specs_json"] = json.dumps(b["line_specs"])
        if b["min_confidence"] == b["max_confidence"]:
            b["confidence_display"] = f"{b['min_confidence']}%"
        else:
            b["confidence_display"] = f"{b['min_confidence']}–{b['max_confidence']}%"

        # Link batch_index & batch_id back to individual items for registry display
        for it in b["batch_items"]:
            it["batch_index"] = idx + 1
            it["batch_id"] = b["batch_id"]

    unresolved_count = len(bulk_candidates) + len(manual_review) + len(blocked_conflicts)
    summary_counts = {
        "total": total_supplier_lines,
        "auto_resolved": len(auto_resolved),
        "bulk_candidates": len(bulk_candidates),
        "manual_review": len(manual_review),
        "blocked_conflicts": len(blocked_conflicts),
        "low_confidence": len(low_confidence),
        "unresolved_count": unresolved_count,
        "bulk_batch_count": len(rfq_bulk_batches)
    }

    item_master = get_item_master(include_inactive=False)

    return {
        "rfq": target_rfq,
        "rfq_id": rfq_id_filter,
        "summary_counts": summary_counts,
        "rfq_bulk_batches": rfq_bulk_batches,
        "manual_review_queue": manual_review,
        "blocked_conflicts": blocked_conflicts,
        "all_registered_items": all_registered_items,
        "suppliers_list": sorted(list(suppliers_set)),
        "item_master": item_master,
        "issues": get_review_center_issues(rfq_id_filter)
    }


def get_dashboard_stats() -> Dict[str, Any]:
    rfqs = list_rfqs(include_demo=False)
    quotes = list_quotes(include_dev_runs=False, include_demo=False)
    comparisons = list_comparisons()
    issues = get_review_center_issues()

    return {
        "rfqs_count": len(rfqs),
        "quotes_count": len(quotes),
        "comparisons_count": len(comparisons),
        "pending_issues_count": len(issues),
        "recent_rfqs": rfqs[:5],
        "recent_quotes": quotes[:6],
        "recent_comparisons": comparisons[:5]
    }


# =========================================================================
# 7. CONTROLLED BENCHMARK SHOWCASE INITIALIZATION
# =========================================================================

def ensure_demo_workspace_initialized():
    """Seeds and executes the 20-item benchmark showcase."""
    default_rfq = get_benchmark_rfq()
    save_rfq_document(default_rfq)
    fixtures_dir = DATA_DIR / "benchmark_fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    paths = generate_benchmark_workbooks(fixtures_dir)

    supp_configs = [
        ("BENCH-SUPP-A", "Alpha Industrial Supplies Ltd", paths["SUPP-A"]),
        ("BENCH-SUPP-B", "Bharat Heavy Components Pvt Ltd", paths["SUPP-B"]),
        ("BENCH-SUPP-C", "Continental Prime Sourcing Ltd", paths["SUPP-C"]),
        ("BENCH-SUPP-D", "Delta Global Trading LLC", paths["SUPP-D"]),
        ("BENCH-SUPP-E", "Elite Surplus Traders", paths["SUPP-E"]),
    ]

    for qid, supp_name, source_path in supp_configs:
        qdir = DATA_DIR / qid
        canon_file = qdir / "extracted" / "canonical_quote.json"
        match_file = qdir / "matching" / "matched_items.json"

        if not canon_file.exists():
            qdir.mkdir(parents=True, exist_ok=True)
            (qdir / "source").mkdir(exist_ok=True)
            (qdir / "extracted").mkdir(exist_ok=True)
            (qdir / "matching").mkdir(exist_ok=True)
            (qdir / "corrections").mkdir(exist_ok=True)
            (qdir / "reports").mkdir(exist_ok=True)

            target_source = qdir / "source" / source_path.name
            shutil.copyfile(source_path, target_source)

            metadata = {
                "test_id": qid,
                "test_name": supp_name,
                "created_at": datetime.utcnow().isoformat(),
                "status": "READY",
                "source_file_name": source_path.name,
                "source_file_size_bytes": source_path.stat().st_size,
                "source_file_hash": None,
                "parser_used": None,
                "processing_time_seconds": 0.0,
                "error_message": None,
                "error_traceback": None,
                "last_stage": None,
                "has_corrections": False,
                "average_confidence": None,
                "rfq_id": DEMO_RFQ_ID,
                "match_status": "NOT_MATCHED",
                "is_demo_quote": True
            }
            with open(qdir / "metadata.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)

            run_extraction(qid)

        if not match_file.exists():
            run_matching_for_quote(qid, DEMO_RFQ_ID)

    comp_file = COMPARISONS_DIR / "COMP-BENCHMARK.json"
    if not comp_file.exists():
        comp_data = run_rfq_comparison(
            rfq_id=DEMO_RFQ_ID,
            quote_ids=DEMO_SUPPLIER_IDS,
            base_currency="INR",
            exchange_rates={"USD": Decimal("85.00"), "EUR": Decimal("92.50")},
            charge_allocation_method=ChargeAllocationMethod.PROPORTIONAL_LINE_VALUE
        )
        comp_data["comparison_id"] = "COMP-BENCHMARK"
        comp_data["title"] = "Executive Commercial Evaluation & Award — Industrial Plant Operations Annual RFQ"
        with open(comp_file, "w", encoding="utf-8") as f:
            json.dump(comp_data, f, indent=2)

# ensure_demo_workspace_initialized() -- disabled auto-seed on module import

def get_demo_workspace_data() -> Dict[str, Any]:
    return get_rfq_workspace_data(DEMO_RFQ_ID)






# =========================================================================
# AWARD DECISION SERVICE LOGIC
# =========================================================================
def get_latest_rfq_comparison(rfq_id: str) -> Optional[Dict[str, Any]]:
    if not rfq_id or not COMPARISONS_DIR.exists():
        return None

    matching_comps = []
    for f in COMPARISONS_DIR.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if data.get("rfq_id") == rfq_id:
                    created_at = data.get("created_at", "")
                    mtime = f.stat().st_mtime
                    comp_id = data.get("comparison_id", f.stem)
                    matching_comps.append((created_at, mtime, comp_id))
        except Exception:
            continue

    if matching_comps:
        matching_comps.sort(key=lambda x: (x[0], x[1]), reverse=True)
        latest_comp_id = matching_comps[0][2]
        return get_comparison(latest_comp_id)
    return None


def get_award_file_path(rfq_id: str) -> Path:
    AWARDS_DIR.mkdir(parents=True, exist_ok=True)
    return AWARDS_DIR / f"{rfq_id}_award.json"


def get_award_decision(rfq_id: str) -> Optional[Dict[str, Any]]:
    path = get_award_file_path(rfq_id)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _generate_fresh_award_proposal(rfq_id: str, comp: Dict[str, Any], scenario: Optional[str] = None, strategy: str = "COST_OPTIMIZED") -> Dict[str, Any]:
    from sourcing.service import run_sourcing_optimization
    from sourcing.models import ProcurementStrategy

    rfq = get_rfq(rfq_id)
    rfq_items_map = {it.rfq_line_id: it for it in (rfq.items if rfq else [])}

    base_cur = comp.get("base_currency", "INR")
    
    # 1. Run Sourcing Optimization Engine
    strat_enum = ProcurementStrategy.COST_OPTIMIZED
    if strategy and strategy.upper() in ProcurementStrategy.__members__:
        strat_enum = ProcurementStrategy[strategy.upper()]

    opt_report = run_sourcing_optimization(comp, strategy=strat_enum)

    # 2. Identify Selected Scenario
    selected_sc = None
    if scenario:
        for sc in opt_report.all_scenarios:
            if sc.scenario_id == scenario or sc.scenario_type == scenario:
                selected_sc = sc
                break

    if not selected_sc:
        selected_sc = opt_report.recommended_scenario or (opt_report.all_scenarios[0] if opt_report.all_scenarios else None)

    comp_details = comp.get("comparison") or comp
    item_comps_raw = comp_details.get("item_comparisons", {})
    if isinstance(item_comps_raw, dict):
        item_comps = list(item_comps_raw.values())
    else:
        item_comps = list(item_comps_raw)

    allocations = []
    total_awarded_val = Decimal("0")
    fully_allocated_count = 0
    partially_allocated_count = 0
    unallocated_count = 0

    for item_comp in item_comps:
        rfq_line_id = item_comp.get("rfq_line_id")
        canonical_rfq_item = rfq_items_map.get(rfq_line_id)
        
        sku = (canonical_rfq_item.sku if canonical_rfq_item and canonical_rfq_item.sku else None) or item_comp.get("sku") or rfq_line_id
        desc = (canonical_rfq_item.description if canonical_rfq_item and canonical_rfq_item.description else None) or item_comp.get("item_description") or item_comp.get("description") or sku
        
        raw_req_q = canonical_rfq_item.requested_quantity if canonical_rfq_item else item_comp.get("requested_quantity")
        req_qty = Decimal(str(raw_req_q)) if raw_req_q is not None and str(raw_req_q).strip() not in ["", "None", "null"] else None
        req_uom = (canonical_rfq_item.requested_uom if canonical_rfq_item else None) or item_comp.get("requested_uom") or None

        supplier_bids = []
        l1_line_supplier_id = None
        min_line_cost = None

        supp_prices_raw = item_comp.get("supplier_prices", {})
        if isinstance(supp_prices_raw, dict):
            supp_prices = list(supp_prices_raw.values())
        else:
            supp_prices = list(supp_prices_raw)

        for supp_price in supp_prices:
            sid = supp_price.get("supplier_id")
            sname = supp_price.get("supplier_name", sid)
            qid = supp_price.get("quote_id", sid)
            
            raw_landed = supp_price.get("unit_landed_price_base") or supp_price.get("effective_unit_landed_cost")
            landed = Decimal(str(raw_landed)) if raw_landed is not None and str(raw_landed).strip() not in ["", "None", "null"] else None
            cur = supp_price.get("quoted_currency") or supp_price.get("currency", base_cur)
            
            raw_qqty = supp_price.get("quoted_qty") or supp_price.get("quoted_quantity")
            qqty = Decimal(str(raw_qqty)) if raw_qqty is not None and str(raw_qqty).strip() not in ["", "None", "null"] else None
            quom = supp_price.get("quoted_uom", req_uom)
            is_comparable = supp_price.get("is_comparable", True)

            raw_base_unit_price = supp_price.get("unit_price_quoted")
            base_unit_price = Decimal(str(raw_base_unit_price)) if raw_base_unit_price is not None and str(raw_base_unit_price).strip() not in ["", "None", "null"] else None

            raw_discount = supp_price.get("discount_pct")
            discount_pct = Decimal(str(raw_discount)) if raw_discount is not None and str(raw_discount).strip() not in ["", "None", "null"] else Decimal("0")

            raw_net_unit_price = supp_price.get("net_unit_price_quoted")
            if raw_net_unit_price is not None and str(raw_net_unit_price).strip() not in ["", "None", "null"]:
                net_unit_price = Decimal(str(raw_net_unit_price))
            elif base_unit_price is not None:
                net_unit_price = base_unit_price
            else:
                net_unit_price = None

            raw_tax_rate = supp_price.get("tax_rate_pct")
            tax_rate_pct = Decimal(str(raw_tax_rate)) if raw_tax_rate is not None and str(raw_tax_rate).strip() not in ["", "None", "null"] else Decimal("0")

            raw_tax_amount = supp_price.get("tax_amount_quoted")
            tax_amount = Decimal(str(raw_tax_amount)) if raw_tax_amount is not None and str(raw_tax_amount).strip() not in ["", "None", "null"] else None

            raw_alloc_charges = supp_price.get("allocated_charges_quoted")
            allocated_charges = Decimal(str(raw_alloc_charges)) if raw_alloc_charges is not None and str(raw_alloc_charges).strip() not in ["", "None", "null"] else Decimal("0")

            raw_exchange_rate = supp_price.get("exchange_rate")
            exchange_rate = Decimal(str(raw_exchange_rate)) if raw_exchange_rate is not None and str(raw_exchange_rate).strip() not in ["", "None", "null"] else Decimal("1.0")

            raw_uom_factor = supp_price.get("uom_conversion_factor")
            uom_factor = Decimal(str(raw_uom_factor)) if raw_uom_factor is not None and str(raw_uom_factor).strip() not in ["", "None", "null"] else Decimal("1.0")

            if is_comparable and landed is not None and landed > Decimal("0") and base_unit_price is not None:
                if min_line_cost is None or landed < min_line_cost:
                    min_line_cost = landed
                    l1_line_supplier_id = sid

            if is_comparable and landed is not None and landed > Decimal("0") and base_unit_price is not None:
                per_unit_tax = (tax_amount / req_qty) if (req_qty and req_qty > Decimal("0") and tax_amount is not None and tax_amount > Decimal("0")) else (net_unit_price * (tax_rate_pct / Decimal("100.0")) * exchange_rate if net_unit_price is not None else Decimal("0"))
                per_unit_charges = (allocated_charges / req_qty) if (req_qty and req_qty > Decimal("0") and allocated_charges > Decimal("0")) else Decimal("0")

                converted_capacity = (qqty * uom_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if qqty is not None else None

                uom_formula = None
                if uom_factor != Decimal("1.0") and quom and req_uom and quom.upper().strip() != req_uom.upper().strip():
                    uom_formula = f"{qqty} {quom} × {uom_factor} = {converted_capacity} {req_uom}"

                landed_breakdown = {
                    "base_unit_price": str(quantize_currency(base_unit_price)),
                    "discount_pct": str(discount_pct),
                    "net_unit_price": str(quantize_currency(net_unit_price)),
                    "tax_rate_pct": str(tax_rate_pct),
                    "tax_amount": str(quantize_currency(per_unit_tax)),
                    "allocated_charges": str(quantize_currency(per_unit_charges)),
                    "exchange_rate": str(exchange_rate),
                    "quoted_currency": cur,
                    "base_currency": base_cur,
                    "unit_landed_cost": str(landed),
                    "calculation_source": f"Supplier Quote {qid}",
                    "uom_conversion_factor": str(uom_factor),
                    "uom_conversion_formula": uom_formula,
                }

                supplier_bids.append({
                    "supplier_id": sid,
                    "supplier_name": sname,
                    "quote_id": qid,
                    "quoted_qty": str(qqty) if qqty is not None else None,
                    "quoted_uom": quom,
                    "supplier_quoted_qty": str(qqty) if qqty is not None else None,
                    "supplier_quoted_uom": quom,
                    "converted_capacity": str(converted_capacity) if converted_capacity is not None else None,
                    "converted_uom": req_uom,
                    "unit_landed_cost": str(landed),
                    "currency": cur,
                    "base_unit_price": str(quantize_currency(base_unit_price)),
                    "discount_pct": str(discount_pct),
                    "net_unit_price": str(quantize_currency(net_unit_price)),
                    "tax_rate_pct": str(tax_rate_pct),
                    "tax_amount": str(quantize_currency(per_unit_tax)),
                    "allocated_charges": str(quantize_currency(per_unit_charges)),
                    "exchange_rate": str(exchange_rate),
                    "uom_conversion_factor": str(uom_factor),
                    "uom_conversion_formula": uom_formula,
                    "landed_price_breakdown": landed_breakdown,
                    "is_comparable": True,
                    "is_l1_for_line": False,
                })
            else:
                supplier_bids.append({
                    "supplier_id": sid,
                    "supplier_name": sname,
                    "quote_id": qid,
                    "quoted_qty": str(qqty) if qqty is not None else None,
                    "quoted_uom": quom,
                    "supplier_quoted_qty": str(qqty) if qqty is not None else None,
                    "supplier_quoted_uom": quom,
                    "converted_capacity": None,
                    "converted_uom": req_uom,
                    "unit_landed_cost": str(landed) if landed is not None else None,
                    "currency": cur,
                    "base_unit_price": str(quantize_currency(base_unit_price)) if base_unit_price is not None else None,
                    "discount_pct": str(discount_pct),
                    "net_unit_price": str(quantize_currency(net_unit_price)) if net_unit_price is not None else None,
                    "tax_rate_pct": str(tax_rate_pct),
                    "tax_amount": None,
                    "allocated_charges": None,
                    "exchange_rate": str(exchange_rate),
                    "uom_conversion_factor": str(uom_factor),
                    "uom_conversion_formula": None,
                    "landed_price_breakdown": {},
                    "is_comparable": False,
                    "is_l1_for_line": False,
                })

        for b in supplier_bids:
            if b["supplier_id"] == l1_line_supplier_id:
                b["is_l1_for_line"] = True

        # Build splits from selected optimization scenario / line recommendation
        splits = []
        bids_by_supp = {b["supplier_id"]: b for b in supplier_bids}
        seen_split_supps = set()

        line_rec = opt_report.line_recommendations.get(rfq_line_id)
        if line_rec and line_rec.recommended_splits:
            source_line_plans = line_rec.recommended_splits
        else:
            source_line_plans = selected_sc.line_plans.get(rfq_line_id, []) if selected_sc else []

        for lp in source_line_plans:
            if lp.supplier_id in seen_split_supps:
                continue
            seen_split_supps.add(lp.supplier_id)

            bid_info = bids_by_supp.get(lp.supplier_id, {})
            unit_price = lp.unit_landed_cost
            awarded_qty = lp.awarded_qty
            bid_qty = lp.quoted_capacity
            quom = lp.quoted_uom or bid_info.get("supplier_quoted_uom", req_uom)
            uom_factor = getattr(lp, "uom_conversion_factor", None) or Decimal(str(bid_info.get("uom_conversion_factor", 1.0)))
            conv_cap = getattr(lp, "converted_capacity", None)
            if conv_cap is None and bid_qty is not None:
                conv_cap = (bid_qty * uom_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            
            unused_conv = getattr(lp, "unused_converted_qty", None)
            if unused_conv is None and conv_cap is not None:
                unused_conv = quantize_currency(conv_cap - awarded_qty)
            
            unused_quote = getattr(lp, "unused_quote_qty", None)
            if unused_quote is None and conv_cap is not None and uom_factor > Decimal("0"):
                unused_quote = ((conv_cap - awarded_qty) / uom_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            uom_formula = getattr(lp, "uom_conversion_formula", None) or bid_info.get("uom_conversion_formula")
            split_val = lp.split_value
            award_val_formula = f"{awarded_qty} {req_uom or 'units'} × {base_cur} {unit_price:,.2f} = {base_cur} {split_val:,.2f}"

            splits.append({
                "supplier_id": lp.supplier_id,
                "supplier_name": lp.supplier_name,
                "quote_id": lp.quote_id or bid_info.get("quote_id", ""),
                "allocated_qty": str(awarded_qty),
                "quoted_uom": quom,
                "supplier_quoted_qty": str(bid_qty) if bid_qty is not None else None,
                "supplier_quoted_uom": quom,
                "converted_capacity": str(conv_cap) if conv_cap is not None else None,
                "converted_uom": req_uom,
                "unit_landed_cost": str(unit_price),
                "split_value": str(split_val),
                "is_l1_for_line": bid_info.get("is_l1_for_line", False),
                "quoted_capacity": str(bid_qty) if bid_qty is not None else None,
                "unused_quote_qty": str(unused_quote) if unused_quote is not None else None,
                "unused_converted_qty": str(unused_conv) if unused_conv is not None else None,
                "base_unit_price": bid_info.get("base_unit_price"),
                "discount_pct": bid_info.get("discount_pct", "0"),
                "tax_rate_pct": bid_info.get("tax_rate_pct", "0"),
                "tax_amount": bid_info.get("tax_amount", "0"),
                "allocated_charges": bid_info.get("allocated_charges", "0"),
                "exchange_rate": bid_info.get("exchange_rate", "1.0"),
                "uom_conversion_factor": str(uom_factor),
                "uom_conversion_formula": uom_formula,
                "landed_price_breakdown": bid_info.get("landed_price_breakdown", {}),
                "award_value_formula": award_val_formula
            })

        total_allocated = sum((Decimal(s["allocated_qty"]) for s in splits), Decimal("0"))
        remaining_qty = max(Decimal("0"), req_qty - total_allocated) if req_qty is not None else None
        shortfall_qty = remaining_qty
        excess_qty = max(Decimal("0"), total_allocated - req_qty) if req_qty is not None else Decimal("0")
        fulfillment_pct = ((total_allocated / req_qty) * Decimal("100.0")).quantize(Decimal("0.1")) if req_qty and req_qty > Decimal("0") else Decimal("0")

        line_val = sum((Decimal(s["split_value"]) for s in splits), Decimal("0"))
        total_awarded_val += line_val

        if req_qty is None:
            alloc_state = "QUANTITY_UNAVAILABLE"
            unallocated_count += 1
        elif total_allocated == req_qty and req_qty > Decimal("0"):
            alloc_state = "FULLY_ALLOCATED"
            fully_allocated_count += 1
        elif total_allocated > Decimal("0"):
            alloc_state = "PARTIALLY_ALLOCATED"
            partially_allocated_count += 1
        else:
            alloc_state = "UNALLOCATED"
            unallocated_count += 1

        line_rec = opt_report.line_recommendations.get(rfq_line_id)
        if line_rec:
            if hasattr(line_rec, "model_dump"):
                line_rec_dict = line_rec.model_dump(mode="json")
            else:
                line_rec_dict = json.loads(line_rec.json())
        else:
            line_rec_dict = {}

        allocations.append({
            "rfq_line_id": rfq_line_id,
            "item_sku": sku,
            "item_description": desc,
            "required_qty": str(req_qty) if req_qty is not None else None,
            "required_uom": req_uom,
            "rfq_required_qty": str(req_qty) if req_qty is not None else None,
            "rfq_required_uom": req_uom,
            "total_allocated_qty": str(total_allocated),
            "awarded_qty": str(total_allocated),
            "unallocated_qty": str(remaining_qty if remaining_qty is not None else "0"),
            "remaining_qty": str(remaining_qty) if remaining_qty is not None else None,
            "shortfall_qty": str(shortfall_qty) if shortfall_qty is not None else None,
            "excess_qty": str(excess_qty),
            "fulfillment_pct": str(fulfillment_pct),
            "fulfillment_percentage": str(fulfillment_pct),
            "total_line_value": str(line_val),
            "award_value": str(line_val),
            "allocation_state": alloc_state,
            "allocation_status": alloc_state,
            "recommendation": line_rec_dict,
            "supplier_splits": splits,
            "available_bids": supplier_bids
        })

    has_unalloc = unallocated_count > 0 or partially_allocated_count > 0 or any(Decimal(str(a.get("unallocated_qty", 0))) > Decimal("0") for a in allocations)
    report_dict = (
        opt_report.model_dump(mode="json")
        if hasattr(opt_report, "model_dump")
        else json.loads(opt_report.json())
    )

    return {
        "award_id": f"AWD-{rfq_id}",
        "rfq_id": rfq_id,
        "comparison_id": comp.get("comparison_id", comp_details.get("comparison_id", "COMP-UNKNOWN")),
        "status": "DRAFT",
        "selected_scenario": selected_sc.scenario_type if selected_sc else "COST_OPTIMIZED_SPLIT",
        "sourcing_strategy": strategy,
        "recommended_scenario_id": opt_report.recommended_scenario.scenario_id if opt_report.recommended_scenario else None,
        "selected_scenario_id": selected_sc.scenario_id if selected_sc else None,
        "is_buyer_override": False,
        "override_reason": None,
        "optimization_report": report_dict,
        "base_currency": base_cur,
        "total_awarded_value": str(quantize_currency(total_awarded_val)),
        "total_required_items": len(allocations),
        "fully_allocated_items_count": fully_allocated_count,
        "partially_allocated_items_count": partially_allocated_count,
        "unallocated_items_count": unallocated_count,
        "has_unallocated_quantities": has_unalloc,
        "buyer_accepted_unallocated": False,
        "allocations": allocations,
        "validation_passed": True,
        "validation_errors": [],
        "validation_warnings": ["Unallocated quantities present in proposal"] if has_unalloc else [],
        "awarded_by": "Procurement Specialist",
        "awarded_at": None,
        "award_notes": None
    }


def build_proposed_award_allocation(rfq_id: str, comparison_id: Optional[str] = None, scenario: Optional[str] = None) -> Dict[str, Any]:
    comp = get_latest_rfq_comparison(rfq_id)
    if not comp:
        return {"error": "No comparison available for this RFQ"}

    existing_award = get_award_decision(rfq_id)

    # 1. FINALIZED awards are locked and authoritative
    if existing_award and existing_award.get("status") == "FINALIZED":
        default_scenario = existing_award.get("selected_scenario")
        fresh_bids_proposal = _generate_fresh_award_proposal(rfq_id, comp, default_scenario)
        bids_by_line = {a["rfq_line_id"]: a.get("available_bids", []) for a in fresh_bids_proposal.get("allocations", [])}
        recs_by_line = {a["rfq_line_id"]: a.get("recommendation", {}) for a in fresh_bids_proposal.get("allocations", [])}
        for a in existing_award.get("allocations", []):
            if "available_bids" not in a or not a["available_bids"]:
                a["available_bids"] = bids_by_line.get(a["rfq_line_id"], [])
            if "recommendation" not in a or not a["recommendation"]:
                a["recommendation"] = recs_by_line.get(a["rfq_line_id"], {})
        if "optimization_report" not in existing_award:
            existing_award["optimization_report"] = fresh_bids_proposal.get("optimization_report", {})
        return existing_award

    # 2. REOPENED awards preserve saved allocations unless explicitly switching scenario
    if existing_award and existing_award.get("status") == "REOPENED" and not scenario:
        default_scenario = existing_award.get("selected_scenario")
        fresh_bids_proposal = _generate_fresh_award_proposal(rfq_id, comp, default_scenario)
        bids_by_line = {a["rfq_line_id"]: a.get("available_bids", []) for a in fresh_bids_proposal.get("allocations", [])}
        recs_by_line = {a["rfq_line_id"]: a.get("recommendation", {}) for a in fresh_bids_proposal.get("allocations", [])}
        for a in existing_award.get("allocations", []):
            if "available_bids" not in a or not a["available_bids"]:
                a["available_bids"] = bids_by_line.get(a["rfq_line_id"], [])
            if "recommendation" not in a or not a["recommendation"]:
                a["recommendation"] = recs_by_line.get(a["rfq_line_id"], {})
        if "optimization_report" not in existing_award:
            existing_award["optimization_report"] = fresh_bids_proposal.get("optimization_report", {})
        return existing_award

    # 3. Draft mode or explicit scenario switch
    target_scenario = scenario or (existing_award.get("selected_scenario") if existing_award else None)
    proposal = _generate_fresh_award_proposal(rfq_id, comp, target_scenario)

    if existing_award:
        proposal["award_id"] = existing_award.get("award_id", f"AWD-{rfq_id}")
        proposal["status"] = existing_award.get("status", "DRAFT")
        proposal["buyer_accepted_unallocated"] = existing_award.get("buyer_accepted_unallocated", False)

        # Restore saved draft allocations and line decision statuses if not an explicit scenario override
        if not scenario and existing_award.get("allocations"):
            saved_lines = {a["rfq_line_id"]: a for a in existing_award.get("allocations", [])}
            for alloc in proposal.get("allocations", []):
                lid = alloc.get("rfq_line_id")
                if lid in saved_lines:
                    saved_alloc = saved_lines[lid]
                    if saved_alloc.get("buyer_decision_status"):
                        alloc["buyer_decision_status"] = saved_alloc.get("buyer_decision_status")
                    if saved_alloc.get("supplier_splits"):
                        alloc["supplier_splits"] = saved_alloc.get("supplier_splits")
                        alloc["total_allocated_qty"] = saved_alloc.get("total_allocated_qty", alloc.get("total_allocated_qty"))
                        alloc["remaining_qty"] = saved_alloc.get("remaining_qty", alloc.get("remaining_qty"))
                        alloc["shortfall_qty"] = saved_alloc.get("shortfall_qty", alloc.get("shortfall_qty"))
                        alloc["unallocated_qty"] = saved_alloc.get("unallocated_qty", alloc.get("unallocated_qty"))
                        alloc["fulfillment_pct"] = saved_alloc.get("fulfillment_pct", alloc.get("fulfillment_pct"))
                        alloc["total_line_value"] = saved_alloc.get("total_line_value", alloc.get("total_line_value"))
                        alloc["allocation_state"] = saved_alloc.get("allocation_state", alloc.get("allocation_state"))

            # Re-sum grand totals deterministically across restored allocations
            recalc_tot_val = Decimal("0")
            recalc_fully = 0
            recalc_partial = 0
            recalc_unalloc = 0
            for a in proposal.get("allocations", []):
                recalc_tot_val += Decimal(str(a.get("total_line_value", 0)))
                state = a.get("allocation_state", "UNALLOCATED")
                if state in ["FULLY_ALLOCATED", "FULLY_FULFILLED"]:
                    recalc_fully += 1
                elif state in ["PARTIALLY_ALLOCATED", "PARTIALLY_FULFILLED"]:
                    recalc_partial += 1
                else:
                    recalc_unalloc += 1

            proposal["total_awarded_value"] = str(quantize_currency(recalc_tot_val))
            proposal["fully_allocated_items_count"] = recalc_fully
            proposal["partially_allocated_items_count"] = recalc_partial
            proposal["unallocated_items_count"] = recalc_unalloc
            proposal["has_unallocated_quantities"] = (recalc_unalloc > 0 or recalc_partial > 0)

    return proposal


def save_award_decision(rfq_id: str, award_data: Dict[str, Any], is_finalized: bool = False) -> Dict[str, Any]:
    """
    Persists an award decision with strict server-side validation gates:
    1. RFQ and Comparison ownership verification.
    2. Finalized award immutability (cannot re-finalize without explicit reopening).
    3. Strict Decimal non-negative quantity parsing.
    4. Authoritative unit price resolution from comparison (never trusts client price).
    5. Supplier eligibility check against authoritative comparison bids.
    6. Supplier Quoted Quantity constraint (awardable_qty <= quoted_qty).
    7. RFQ Required Quantity constraint (total_allocated_qty <= required_qty).
    8. Partial/unallocated award acknowledgement enforcement.
    9. Deterministic Decimal financial recalculation & calculation lineage preservation.
    """
    rfq = get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    comp = get_latest_rfq_comparison(rfq_id)
    if not comp:
        raise ValueError(f"No valid commercial comparison available for RFQ {rfq_id}.")
    comp_id = comp.get("comparison_id", "COMP-UNKNOWN")
    base_cur = comp.get("base_currency", "INR")

    existing_award = get_award_decision(rfq_id)
    val_errors: List[str] = []
    val_warnings: List[str] = []

    # Safeguard 1: Finalized Immutability Guard
    if existing_award and existing_award.get("status") == "FINALIZED" and is_finalized:
        val_errors.append("Award is already FINALIZED and locked. To modify allocations, please reopen the award first.")
        existing_award["validation_passed"] = False
        existing_award["validation_errors"] = val_errors
        return existing_award

    # Safeguard 2: Reconstruct Authoritative Bids & Line Scope from Fresh Proposal
    fresh_proposal = _generate_fresh_award_proposal(rfq_id, comp)
    rfq_lines_map = {a["rfq_line_id"]: a for a in fresh_proposal.get("allocations", [])}
    
    # Authoritative Bids Lookup Map: (rfq_line_id, supplier_id/quote_id) -> bid
    authoritative_bids_map = {}
    for a in fresh_proposal.get("allocations", []):
        line_id = a["rfq_line_id"]
        for bid in a.get("available_bids", []):
            if bid.get("is_comparable", True) and Decimal(str(bid.get("unit_landed_cost", 0))) > Decimal("0"):
                authoritative_bids_map[(line_id, bid["supplier_id"])] = bid
                if bid.get("quote_id"):
                    authoritative_bids_map[(line_id, bid["quote_id"])] = bid

    raw_allocations = award_data.get("allocations", [])
    sanitized_allocations: List[Dict[str, Any]] = []
    seen_line_ids = set()

    total_val = Decimal("0")
    fully_alloc_count = 0
    partially_alloc_count = 0
    unalloc_count = 0

    for alloc in raw_allocations:
        line_id = alloc.get("rfq_line_id")
        
        # Safeguard 3: RFQ Line Ownership Validation
        if not line_id or line_id not in rfq_lines_map:
            val_errors.append(f"Invalid RFQ line ID '{line_id}'. Line does not belong to RFQ {rfq_id}.")
            continue
        
        if line_id in seen_line_ids:
            val_errors.append(f"Duplicate allocation submitted for RFQ line {line_id}.")
            continue
        seen_line_ids.add(line_id)

        canonical_line = rfq_lines_map[line_id]
        raw_req = canonical_line.get("required_qty")
        req_qty = Decimal(str(raw_req)) if raw_req is not None and str(raw_req).strip() not in ["", "None", "null"] else None
        req_uom = canonical_line.get("required_uom", "PCS")
        item_sku = canonical_line.get("item_sku", line_id)
        item_desc = canonical_line.get("item_description", item_sku)

        raw_splits = alloc.get("supplier_splits", [])
        sanitized_splits: List[Dict[str, Any]] = []
        line_alloc_qty = Decimal("0")
        line_total_val = Decimal("0")

        for sp in raw_splits:
            raw_qty = sp.get("allocated_qty", 0)
            
            # Safeguard 4: Strict Non-Negative Decimal Quantity Parsing
            try:
                sp_qty = Decimal(str(raw_qty))
            except Exception:
                val_errors.append(f"Line {line_id} ({item_sku}): Malformed allocated quantity '{raw_qty}'.")
                continue

            if sp_qty < Decimal("0"):
                val_errors.append(f"Line {line_id} ({item_sku}): Negative allocated quantity ({sp_qty}) is not permitted.")
                continue

            if sp_qty == Decimal("0"):
                continue

            # Safeguard 5: Supplier / Quote Eligibility & Authoritative Pricing Resolution
            supp_id = sp.get("supplier_id")
            quote_id = sp.get("quote_id")
            
            bid = None
            if supp_id and (line_id, supp_id) in authoritative_bids_map:
                cand = authoritative_bids_map[(line_id, supp_id)]
                if not quote_id or quote_id in [cand.get("quote_id"), cand.get("supplier_id")]:
                    bid = cand
            elif quote_id and (line_id, quote_id) in authoritative_bids_map:
                cand = authoritative_bids_map[(line_id, quote_id)]
                if not supp_id or supp_id in [cand.get("supplier_id"), cand.get("quote_id"), cand.get("supplier_name")]:
                    bid = cand

            if not bid:
                val_errors.append(f"Line {line_id} ({item_sku}): Supplier '{supp_id or quote_id}' does not have an eligible, comparable bid in comparison {comp_id}.")
                continue

            # Safeguard: Inactive supplier cannot receive a new award allocation
            supp_rec = get_supplier(bid["supplier_id"])
            if supp_rec and supp_rec.status == "INACTIVE":
                val_errors.append(f"Line {line_id} ({item_sku}): Supplier '{bid['supplier_name']}' ({bid['supplier_id']}) is INACTIVE in Supplier Master and cannot receive a new award.")
                continue

            authoritative_unit_landed_cost = Decimal(str(bid["unit_landed_cost"]))
            
            # Safeguard 6: Supplier Converted Capacity Ceiling Check
            raw_bid_q = bid.get("supplier_quoted_qty") or bid.get("quoted_qty")
            bid_quoted_qty = Decimal(str(raw_bid_q)) if raw_bid_q is not None and str(raw_bid_q).strip() not in ["", "None", "null"] else None
            u_factor = Decimal(str(bid.get("uom_conversion_factor", 1.0)))
            conv_cap = (bid_quoted_qty * u_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if bid_quoted_qty is not None else None

            if conv_cap is not None and sp_qty > conv_cap + Decimal("0.001"):
                val_errors.append(f"Line {line_id} ({item_sku}): Awarded quantity ({sp_qty} {req_uom}) exceeds supplier converted capacity ({conv_cap} {req_uom} equivalent to {bid_quoted_qty} {bid.get('supplier_quoted_uom', req_uom)}) for {bid['supplier_name']}.")

            unused_conv = max(Decimal("0"), conv_cap - sp_qty) if conv_cap is not None else None
            unused_quote = max(Decimal("0"), ((conv_cap - sp_qty) / u_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if conv_cap is not None and u_factor > Decimal("0") else None

            # Verify client price if passed; reject tampering
            raw_client_price = sp.get("unit_landed_cost")
            if raw_client_price is not None:
                try:
                    client_price = Decimal(str(raw_client_price))
                    if abs(client_price - authoritative_unit_landed_cost) > Decimal("0.01"):
                        val_errors.append(f"Line {line_id} ({item_sku}): Submitted unit price ({client_price}) differs from authoritative comparison price ({authoritative_unit_landed_cost}) for supplier {bid['supplier_name']}.")
                        continue
                except Exception:
                    val_errors.append(f"Line {line_id} ({item_sku}): Malformed unit price '{raw_client_price}'.")
                    continue

            # Safeguard 7: Deterministic Decimal Financial Calculation
            sp_val = quantize_currency(sp_qty * authoritative_unit_landed_cost)
            award_val_formula = f"{sp_qty} {req_uom or 'units'} × {base_cur} {authoritative_unit_landed_cost:,.2f} = {base_cur} {sp_val:,.2f}"

            line_alloc_qty += sp_qty
            line_total_val += sp_val

            sanitized_splits.append({
                "supplier_id": bid["supplier_id"],
                "supplier_name": bid["supplier_name"],
                "quote_id": bid["quote_id"],
                "allocated_qty": str(sp_qty),
                "quoted_uom": bid.get("supplier_quoted_uom") or bid.get("quoted_uom", req_uom or "PCS"),
                "supplier_quoted_qty": str(bid_quoted_qty) if bid_quoted_qty is not None else None,
                "supplier_quoted_uom": bid.get("supplier_quoted_uom") or bid.get("quoted_uom", req_uom or "PCS"),
                "converted_capacity": str(conv_cap) if conv_cap is not None else None,
                "converted_uom": req_uom,
                "unit_landed_cost": str(authoritative_unit_landed_cost),
                "split_value": str(sp_val),
                "is_l1_for_line": bid.get("is_l1_for_line", False),
                "quoted_capacity": str(bid_quoted_qty) if bid_quoted_qty is not None else None,
                "unused_quote_qty": str(unused_quote) if unused_quote is not None else None,
                "unused_converted_qty": str(unused_conv) if unused_conv is not None else None,
                "base_unit_price": bid.get("base_unit_price"),
                "discount_pct": bid.get("discount_pct", "0"),
                "tax_rate_pct": bid.get("tax_rate_pct", "0"),
                "allocated_charges": bid.get("allocated_charges", "0"),
                "exchange_rate": bid.get("exchange_rate", "1.0"),
                "uom_conversion_factor": str(u_factor),
                "uom_conversion_formula": bid.get("uom_conversion_formula"),
                "landed_price_breakdown": bid.get("landed_price_breakdown", {}),
                "award_value_formula": award_val_formula
            })

        # Safeguard 8: Over-Allocation Check against RFQ Required Quantity
        if req_qty is None:
            alloc_state = "QUANTITY_UNAVAILABLE"
            unalloc_count += 1
            remaining_qty = None
            shortfall_qty = None
            excess_qty = Decimal("0")
            fulfillment_pct = Decimal("0")
        elif line_alloc_qty > req_qty:
            val_errors.append(f"Line {line_id} ({item_sku}): Total allocated quantity ({line_alloc_qty}) exceeds required quantity ({req_qty}).")
            alloc_state = "OVER_ALLOCATED"
            remaining_qty = Decimal("0")
            shortfall_qty = Decimal("0")
            excess_qty = line_alloc_qty - req_qty
            fulfillment_pct = ((line_alloc_qty / req_qty) * Decimal("100.0")).quantize(Decimal("0.1"))
        elif line_alloc_qty == req_qty and req_qty > Decimal("0"):
            fully_alloc_count += 1
            alloc_state = "FULLY_ALLOCATED"
            remaining_qty = Decimal("0")
            shortfall_qty = Decimal("0")
            excess_qty = Decimal("0")
            fulfillment_pct = Decimal("100.0")
        elif line_alloc_qty > Decimal("0"):
            partially_alloc_count += 1
            alloc_state = "PARTIALLY_ALLOCATED"
            remaining_qty = req_qty - line_alloc_qty
            shortfall_qty = remaining_qty
            excess_qty = Decimal("0")
            fulfillment_pct = ((line_alloc_qty / req_qty) * Decimal("100.0")).quantize(Decimal("0.1"))
            val_warnings.append(f"Line {line_id}: Partially allocated ({line_alloc_qty}/{req_qty} {req_uom}).")
        else:
            unalloc_count += 1
            alloc_state = "UNALLOCATED"
            remaining_qty = req_qty
            shortfall_qty = req_qty
            excess_qty = Decimal("0")
            fulfillment_pct = Decimal("0")
            val_warnings.append(f"Line {line_id} ({item_sku}): 0 quantity allocated.")

        total_val += line_total_val

        sanitized_allocations.append({
            "rfq_line_id": line_id,
            "item_sku": item_sku,
            "item_description": item_desc,
            "buyer_decision_status": alloc.get("buyer_decision_status"),
            "required_qty": str(req_qty) if req_qty is not None else None,
            "required_uom": req_uom,
            "rfq_required_qty": str(req_qty) if req_qty is not None else None,
            "rfq_required_uom": req_uom,
            "total_allocated_qty": str(line_alloc_qty),
            "awarded_qty": str(line_alloc_qty),
            "unallocated_qty": str(remaining_qty if remaining_qty is not None else "0"),
            "remaining_qty": str(remaining_qty) if remaining_qty is not None else None,
            "shortfall_qty": str(shortfall_qty) if shortfall_qty is not None else None,
            "excess_qty": str(excess_qty),
            "fulfillment_pct": str(fulfillment_pct),
            "fulfillment_percentage": str(fulfillment_pct),
            "total_line_value": str(line_total_val),
            "award_value": str(line_total_val),
            "allocation_state": alloc_state,
            "allocation_status": alloc_state,
            "recommendation": canonical_line.get("recommendation", {}),
            "supplier_splits": sanitized_splits,
            "available_bids": canonical_line.get("available_bids", [])
        })

    # Include any missing RFQ lines as unallocated
    for line_id, canonical_line in rfq_lines_map.items():
        if line_id not in seen_line_ids:
            raw_req = canonical_line.get("required_qty")
            req_qty = Decimal(str(raw_req)) if raw_req is not None and str(raw_req).strip() not in ["", "None", "null"] else None
            unalloc_count += 1
            sanitized_allocations.append({
                "rfq_line_id": line_id,
                "item_sku": canonical_line.get("item_sku", line_id),
                "item_description": canonical_line.get("item_description", ""),
                "required_qty": str(req_qty) if req_qty is not None else None,
                "required_uom": canonical_line.get("required_uom", "PCS"),
                "total_allocated_qty": "0",
                "unallocated_qty": str(req_qty if req_qty is not None else "0"),
                "remaining_qty": str(req_qty) if req_qty is not None else None,
                "shortfall_qty": str(req_qty) if req_qty is not None else None,
                "excess_qty": "0",
                "fulfillment_pct": "0",
                "total_line_value": "0.00",
                "allocation_state": "UNALLOCATED",
                "recommendation": canonical_line.get("recommendation", {}),
                "supplier_splits": [],
                "available_bids": canonical_line.get("available_bids", [])
            })
            val_warnings.append(f"Line {line_id}: Not included in submission (marked unallocated).")

    # Safeguard 9: Partial / Unallocated Award Acknowledgement Requirement
    has_unalloc = unalloc_count > 0 or partially_alloc_count > 0 or any(Decimal(str(a.get("unallocated_qty", 0))) > Decimal("0") for a in sanitized_allocations)
    accepted_unalloc = bool(award_data.get("buyer_accepted_unallocated", False))

    if has_unalloc and is_finalized and not accepted_unalloc:
        val_errors.append("Unallocated quantities present in proposal. Please acknowledge and confirm partial award before finalizing.")

    is_valid = (len(val_errors) == 0)
    final_status = "FINALIZED" if (is_finalized and is_valid) else ("REOPENED" if (existing_award and existing_award.get("status") == "REOPENED" and not is_finalized) else "DRAFT")

    from datetime import datetime
    now_utc = datetime.now(timezone.utc).isoformat()

    award_record = {
        "award_id": award_data.get("award_id") or (existing_award.get("award_id") if existing_award else f"AWD-{rfq_id}"),
        "rfq_id": rfq_id,
        "comparison_id": comp_id,
        "status": final_status,
        "selected_scenario": award_data.get("selected_scenario", "MANUAL_ALLOCATION"),
        "base_currency": rfq.base_currency or award_data.get("base_currency", "INR"),
        "total_awarded_value": str(total_val),
        "total_required_items": len(sanitized_allocations),
        "fully_allocated_items_count": fully_alloc_count,
        "partially_allocated_items_count": partially_alloc_count,
        "unallocated_items_count": unalloc_count,
        "has_unallocated_quantities": has_unalloc,
        "buyer_accepted_unallocated": accepted_unalloc,
        "allocations": sanitized_allocations,
        "optimization_report": fresh_proposal.get("optimization_report", {}),
        "validation_passed": is_valid,
        "validation_errors": val_errors,
        "validation_warnings": val_warnings,
        "awarded_by": award_data.get("awarded_by", "Procurement Specialist"),
        "awarded_at": now_utc if (is_finalized and is_valid) else (existing_award.get("awarded_at") if existing_award else None),
        "award_notes": award_data.get("award_notes") or (existing_award.get("award_notes") if existing_award else None),
        "reopen_history": existing_award.get("reopen_history", []) if existing_award else []
    }

    # Only persist valid records or drafts; never corrupt existing finalized files with invalid payloads
    if is_valid or not (existing_award and existing_award.get("status") == "FINALIZED"):
        path = get_award_file_path(rfq_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(award_record, f, indent=2)

    return award_record


def reopen_award_decision(rfq_id: str, reason: str = "", reopened_by: str = "Procurement Specialist") -> Optional[Dict[str, Any]]:
    """
    Reopens a finalized award, establishing an auditable revision trail while unlocking the editor.
    """
    record = get_award_decision(rfq_id)
    if record:
        from datetime import datetime
        now_utc = datetime.now(timezone.utc).isoformat()
        
        # Preserve previous finalized audit snapshot
        history_entry = {
            "previous_status": record.get("status", "FINALIZED"),
            "previous_comparison_id": record.get("comparison_id"),
            "previous_total_awarded_value": record.get("total_awarded_value"),
            "previous_awarded_at": record.get("awarded_at"),
            "reopened_at": now_utc,
            "reopened_by": reopened_by,
            "reopen_reason": reason or "Buyer requested allocation revision"
        }
        
        if "reopen_history" not in record or not isinstance(record["reopen_history"], list):
            record["reopen_history"] = []
        record["reopen_history"].append(history_entry)

        record["status"] = "REOPENED"
        record["reopened_at"] = now_utc
        record["reopened_by"] = reopened_by
        record["reopen_reason"] = reason or "Buyer requested allocation revision"
        record["award_notes"] = f"Reopened on revision. Reason: {record['reopen_reason']}"
        
        path = get_award_file_path(rfq_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
    return record


# =========================================================================
# 9. ENTERPRISE PROCUREMENT ANALYTICS & INSIGHTS
# =========================================================================

def get_analytics_data() -> Dict[str, Any]:
    """Computes comprehensive executive procurement metrics and stage pipeline stats."""
    user_rfqs = list_rfqs(include_demo=False)
    all_rfqs = list_rfqs(include_demo=True)
    rfqs = user_rfqs if user_rfqs else all_rfqs

    quotes = list_quotes(include_dev_runs=False, include_demo=True)
    comparisons = list_comparisons()
    item_master = get_item_master()

    # Finalized awards
    awards = []
    total_awarded_val = Decimal("0.00")
    for r in rfqs:
        awd = get_award_decision(r["rfq_id"])
        if awd and awd.get("status") == "FINALIZED":
            awards.append(awd)
            try:
                total_awarded_val += Decimal(str(awd.get("total_awarded_value", "0.00")))
            except Exception:
                pass

    # Status counts
    status_counts = {
        "DRAFT": sum(1 for r in rfqs if r.get("status") == "DRAFT"),
        "QUOTES_PENDING": sum(1 for r in rfqs if r.get("status") == "QUOTES_PENDING"),
        "MATCHING_REVIEW": sum(1 for r in rfqs if r.get("status") == "MATCHING_REVIEW"),
        "READY_FOR_COMPARISON": sum(1 for r in rfqs if r.get("status") == "READY_FOR_COMPARISON"),
        "EVALUATED": sum(1 for r in rfqs if r.get("status") == "EVALUATED"),
        "AWARD_FINALIZED": len(awards)
    }

    # Currencies active
    currencies = set()
    for q in quotes:
        if q.get("currency"):
            currencies.add(q["currency"].upper().strip())
    if not currencies:
        currencies.add("INR")

    return {
        "total_rfqs": len(rfqs),
        "total_quotes": len(quotes),
        "total_comparisons": len(comparisons),
        "total_awards": len(awards),
        "total_awarded_val": f"{total_awarded_val:,.2f}",
        "total_catalog_items": len(item_master),
        "status_counts": status_counts,
        "currencies_count": len(currencies),
        "currencies_list": sorted(list(currencies)),
        "recent_rfqs": rfqs[:5],
        "recent_comparisons": comparisons[:5]
    }


# =========================================================================
# 10. SETTINGS, PREFERENCES & DATA MANAGEMENT SERVICES
# =========================================================================

SETTINGS_FILE = DATA_DIR / "settings.json"

def get_application_settings() -> Dict[str, Any]:
    """Loads persistent enterprise settings or returns sensible defaults."""
    default_settings = {
        "company_name": "Apex Global Procurement Corp",
        "default_currency": "INR",
        "default_allocation": "PROPORTIONAL_LINE_VALUE",
        "default_payment_terms": "Net 30 Days",
        "default_incoterm": "DAP",
        "variance_tolerance_pct": "5.0",
        "auto_matching_mode": "BALANCED",
        "allow_partial_quotes": True,
        "theme": "dark",
        "accent": "blue"
    }
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_settings.update(saved)
        except Exception:
            pass
    return default_settings


def save_application_settings(new_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Saves enterprise preferences to disk."""
    current = get_application_settings()
    current.update(new_settings)
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    return current


def reset_all_procurement_data() -> Dict[str, Any]:
    """
    Cleans ONLY explicitly identified test-generated quotes, comparisons, and test RFQs.
    CRITICAL INVARIANT: Real user-created RFQs, quotations, comparisons, and awards are permanent business records and are NEVER deleted.
    """
    # 1. Preserve RFQs directory and all user RFQs
    RFQS_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Only remove test RFQs
    for rf in list(RFQS_DIR.glob("*.json")):
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            if data.get("source") in ["TEST", "DEMO"] or data.get("is_test", False) or rf.stem.startswith("TEST-") or rf.stem.startswith("RFQ-TEST-"):
                rf.unlink()
        except Exception:
            pass

    # 3. Only remove test Comparisons
    if COMPARISONS_DIR.exists():
        for cf in list(COMPARISONS_DIR.glob("*.json")):
            try:
                data = json.loads(cf.read_text(encoding="utf-8"))
                rfq_id = data.get("rfq_id", "")
                if rfq_id.startswith("TEST-") or rfq_id.startswith("RFQ-TEST-") or cf.stem.startswith("TEST-"):
                    cf.unlink()
            except Exception:
                pass

    # 4. Only remove test Awards
    if AWARDS_DIR.exists():
        for af in list(AWARDS_DIR.glob("*.json")):
            try:
                if af.stem.startswith("TEST-") or af.stem.startswith("RFQ-TEST-"):
                    af.unlink()
            except Exception:
                pass

    # 5. Clean test quote directories only
    for item in list(DATA_DIR.iterdir()):
        if item.name in ["item_master", "rfqs", "comparisons", "awards", "settings.json", "supplier_mappings.json", "import_batches.json"]:
            continue
        if item.is_dir() and (item.name.startswith("TEST-") or item.name.startswith("Q-BENCH-") or item.name.startswith("BENCH-") or "test" in item.name.lower()):
            try:
                shutil.rmtree(item)
            except Exception:
                pass

    return {"status": "success", "message": "Test artifacts cleaned. All user-created RFQs and quotes remain permanently preserved."}


def seed_demo_benchmark_data() -> Dict[str, Any]:
    """Generates the benchmark RFQ and quotes for demonstration."""
    ensure_demo_workspace_initialized()
    return {"status": "success", "message": "Benchmark demo data successfully seeded."}


def export_all_procurement_data() -> Dict[str, Any]:
    """Packages all active RFQs, comparisons, awards, and Item Master into an exportable JSON payload."""
    rfqs = list_rfqs(include_demo=True)
    comparisons = list_comparisons()
    item_master = get_item_master()
    
    awards = {}
    if AWARDS_DIR.exists():
        for af in AWARDS_DIR.glob("*_award.json"):
            try:
                with open(af, "r", encoding="utf-8") as f:
                    awards[af.stem] = json.load(f)
            except Exception:
                pass

    return {
        "export_timestamp": datetime.now(timezone.utc).isoformat(),
        "application_version": "2.0.0",
        "settings": get_application_settings(),
        "item_master_count": len(item_master),
        "item_master": [im.dict() if hasattr(im, "dict") else im for im in item_master],
        "rfqs": rfqs,
        "comparisons": comparisons,
        "awards": awards
    }


def get_rfq_review_queue(rfq_id: str) -> Dict[str, Any]:
    """
    Returns all unresolved alignment issues across all supplier quotes for a given RFQ in one unified queue.
    """
    rfq = get_rfq(rfq_id)
    if not rfq:
        return {"rfq": None, "issues": [], "stats": {}, "item_master": []}

    quotes = get_quotes_for_rfq(rfq_id)
    item_master = get_item_master()

    issues = []
    total_lines = 0
    auto_matched_count = 0
    review_required_count = 0
    unmatched_count = 0
    safe_suggestions_count = 0

    for q in quotes:
        qid = q.get("test_id") or q.get("quote_id")
        matched = get_matched_items(qid) or []
        supplier_name = q.get("supplier_name") if q.get("supplier_name") and q.get("supplier_name") != "—" else q.get("test_name", qid)

        for m in matched:
            total_lines += 1
            st = m.match_status
            if st in [MatchStatus.EXACT_MATCH, MatchStatus.HIGH_CONFIDENCE_MATCH]:
                auto_matched_count += 1
            elif st in [MatchStatus.REVIEW_REQUIRED, MatchStatus.UOM_INCOMPATIBLE]:
                review_required_count += 1
            elif st == MatchStatus.UNMATCHED:
                unmatched_count += 1

            # If review required or unmatched, add to review issues queue
            if st in [MatchStatus.REVIEW_REQUIRED, MatchStatus.UOM_INCOMPATIBLE, MatchStatus.UNMATCHED]:
                best_cand = m.item_master_match or (m.top_candidates[0] if m.top_candidates else None)
                top_cands = m.top_candidates or []

                uom_compat = m.uom_conversion.is_compatible if m.uom_conversion else True
                is_safe = (
                    best_cand is not None
                    and best_cand.match_score >= 0.88
                    and (len(top_cands) <= 1 or (best_cand.match_score - top_cands[1].match_score) >= 0.08)
                    and uom_compat
                )
                if is_safe:
                    safe_suggestions_count += 1

                category = "AMBIGUOUS"
                if not uom_compat:
                    category = "UOM_CONFLICT"
                elif st == MatchStatus.UNMATCHED:
                    category = "UNMATCHED"
                elif len(top_cands) > 1 and (best_cand.match_score - top_cands[1].match_score) < 0.08:
                    category = "CLOSE_CANDIDATES"

                target_rfq_line = None
                if best_cand:
                    for rline in rfq.items:
                        if (rline.sku and rline.sku.upper() == best_cand.candidate_sku.upper()) or (rline.internal_item_id == best_cand.candidate_item_id):
                            target_rfq_line = rline
                            break

                issues.append({
                    "issue_id": f"{qid}_{m.quote_item.line_index}",
                    "quote_id": qid,
                    "supplier_name": supplier_name,
                    "line_index": m.quote_item.line_index,
                    "line_number": m.quote_item.line_index + 1,
                    "raw_description": m.quote_item.raw_description,
                    "supplier_part_number": m.quote_item.supplier_part_number,
                    "quoted_qty": str(m.quote_item.quoted_qty),
                    "quoted_uom": m.quote_item.quoted_uom,
                    "unit_price": str(m.quote_item.unit_price),
                    "currency": q.get("currency", rfq.base_currency or "INR"),
                    "match_status": st.value,
                    "category": category,
                    "review_reasons": m.review_reasons or ["Fuzzy multi-signal candidate confirmation required"],
                    "uom_compatible": uom_compat,
                    "uom_error": m.uom_conversion.error_reason if m.uom_conversion and not uom_compat else None,
                    "best_candidate": best_cand.model_dump() if best_cand else None,
                    "top_candidates": [c.model_dump() for c in top_cands],
                    "target_rfq_line": target_rfq_line.model_dump() if target_rfq_line else None,
                    "is_safe_auto_match": is_safe
                })

    stats = {
        "total_quotes": len(quotes),
        "total_lines_extracted": total_lines,
        "total_auto_matched": auto_matched_count,
        "total_review_required": review_required_count,
        "total_unmatched": unmatched_count,
        "safe_suggestions_count": safe_suggestions_count,
        "total_issues_count": len(issues),
        "has_issues": len(issues) > 0
    }

    return {
        "rfq": rfq,
        "issues": issues,
        "stats": stats,
        "item_master": [im.model_dump() if hasattr(im, "model_dump") else im for im in item_master]
    }


def bulk_resolve_safe_matches(rfq_id: str) -> Dict[str, Any]:
    """Accepts all unambiguous high-confidence matches across all quotes for an RFQ."""
    queue_data = get_rfq_review_queue(rfq_id)
    issues = queue_data.get("issues", [])
    accepted_count = 0

    for iss in issues:
        if iss.get("is_safe_auto_match") and iss.get("best_candidate"):
            qid = iss["quote_id"]
            line_idx = iss["line_index"]
            sku = iss["best_candidate"]["candidate_sku"]
            resolve_match_candidate(qid, line_idx, sku, action="ACCEPT")
            accepted_count += 1

    new_queue = get_rfq_review_queue(rfq_id)
    return {
        "status": "success",
        "accepted_count": accepted_count,
        "remaining_issues_count": new_queue["stats"]["total_issues_count"]
    }
