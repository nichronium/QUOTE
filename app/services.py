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
from decimal import Decimal
import json
from pathlib import Path
import shutil
import time
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
from matching.models import ItemMasterRecord, MatchCandidate, MatchedQuoteItem, MatchMethod, MatchStatus, RFQLineItem
from parsers.csv_parser import CSVParser
from parsers.excel_parser import ExcelParser
from parsers.pdf_parser import PDFParser


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "test_runs"
RFQS_DIR = DATA_DIR / "rfqs"
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
    """Seeds the benchmark RFQ into the system if none exists."""
    default_rfq = get_benchmark_rfq()
    rfq_file = RFQS_DIR / f"{default_rfq.rfq_id}.json"
    if not rfq_file.exists():
        with open(rfq_file, "w", encoding="utf-8") as f:
            f.write(default_rfq.model_dump_json(indent=2))


ensure_default_rfqs()


def list_rfqs(include_demo: bool = False) -> List[Dict[str, Any]]:
    """
    Lists RFQs. By default (include_demo=False), only returns user-created RFQs
    so the normal procurement workspace starts completely clean.
    """
    ensure_default_rfqs()
    rfqs = []
    all_comparisons = list_comparisons()
    all_quotes = list_quotes(include_dev_runs=True, include_demo=True)
    for f in sorted(RFQS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                rfq = RFQDocument.model_validate(data)
                
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

                # Determine RFQ state
                if award_rec and award_rec.get("status") == "FINALIZED":
                    status = "AWARD_FINALIZED"
                elif comp_list:
                    status = "EVALUATED"
                elif len(attached_quotes) == 0:
                    status = "DRAFT"
                elif has_review:
                    status = "MATCHING_REVIEW"
                elif ready_quotes >= 2:
                    status = "READY_FOR_COMPARISON"
                else:
                    status = "QUOTES_PENDING"

                rfqs.append({
                    "rfq_id": rfq.rfq_id,
                    "title": rfq.title,
                    "base_currency": rfq.base_currency,
                    "line_item_count": len(rfq.items),
                    "quote_count": len(attached_quotes),
                    "ready_quote_count": ready_quotes,
                    "status": status,
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


def create_rfq(rfq_id: str, title: str, base_currency: str, items: List[Dict[str, Any]]) -> RFQDocument:
    # 1. Auto-register any new items into company Item Master so catalog stays unified and supplier quotes match seamlessly
    current_im = get_item_master()
    existing_skus = {im.internal_sku.upper().strip() for im in current_im if im.internal_sku}
    existing_ids = {im.internal_item_id for im in current_im if im.internal_item_id}
    new_im_records = []

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
                brand="Standard"
            ))
            existing_skus.add(sku)
            existing_ids.add(item_id)

    if new_im_records:
        current_im.extend(new_im_records)
        save_item_master(current_im)

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

    rfq = RFQDocument(
        rfq_id=rfq_id.strip() or f"RFQ-{int(time.time())}",
        title=title.strip(),
        base_currency=base_currency.strip().upper() or "INR",
        items=rfq_items
    )
    with open(RFQS_DIR / f"{rfq.rfq_id}.json", "w", encoding="utf-8") as f:
        f.write(rfq.model_dump_json(indent=2))
    return rfq


def ensure_item_master_initialized():
    """Seeds canonical Item Master into catalog.json if it doesn't exist."""
    catalog_file = ITEM_MASTER_DIR / "catalog.json"
    if not catalog_file.exists():
        benchmark_items = get_benchmark_item_master()
        with open(catalog_file, "w", encoding="utf-8") as f:
            json.dump([item.model_dump(mode="json") for item in benchmark_items], f, indent=2)


ensure_item_master_initialized()


def get_item_master() -> List[ItemMasterRecord]:
    """Returns company Item Master catalog records."""
    catalog_file = ITEM_MASTER_DIR / "catalog.json"
    if not catalog_file.exists():
        ensure_item_master_initialized()
    try:
        with open(catalog_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [ItemMasterRecord.model_validate(d) for d in data]
    except Exception:
        return get_benchmark_item_master()


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
    items = get_item_master()
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
        specifications=specifications or {}
    )
    items.append(record)
    save_item_master(items)
    return record


def get_item_master_item(item_id: str) -> Optional[ItemMasterRecord]:
    """Retrieves a single item from the company Item Master catalog."""
    items = get_item_master()
    for it in items:
        if it.internal_item_id == item_id or it.internal_sku == item_id:
            return it
    return None


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


def commit_catalog_import(items_data: List[Dict[str, Any]], mode: str = "append") -> int:
    """
    Validates and commits a list of parsed preview items into the company Item Master catalog.
    mode can be 'append' (adds to existing) or 'replace' (replaces catalog).
    """
    existing_items = [] if mode == "replace" else get_item_master()
    
    existing_ids = [it.internal_item_id for it in existing_items if it.internal_item_id.startswith("ITEM-")]
    indices = []
    for i in existing_ids:
        try:
            indices.append(int(i.replace("ITEM-", "")))
        except ValueError:
            pass
    next_idx = max(indices, default=0) + 1

    committed_count = 0
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
            specifications=specs
        )
        existing_items.append(rec)
        committed_count += 1

    save_item_master(existing_items)
    return committed_count


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


def run_matching_for_quote(quote_id: str, rfq_id: Optional[str] = None) -> List[MatchedQuoteItem]:
    """
    Runs multi-signal item matching for a quote while strictly preserving locked human decisions.
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
                            explanations=["Confirmed by procurement reviewer"],
                            matched_fields=["human_confirmation"],
                            source_provenance=em.quote_item.provenance
                        )
                        break
            human_resolved_by_line[em.quote_item.line_index] = em

    # 2. Run automated matching on all quote items
    item_master = get_item_master()
    matcher = ItemMatcher()
    fresh_matches = matcher.match_quote(quote, item_master, rfq.items if rfq else None)

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


def resolve_match_candidate(quote_id: str, line_index: int, chosen_candidate_sku: Optional[str], action: str = "ACCEPT") -> List[MatchedQuoteItem]:
    matched_items = get_matched_items(quote_id)
    if not matched_items:
        raise ValueError(f"No matched items found for quote {quote_id}.")

    target = next((m for m in matched_items if m.quote_item.line_index == line_index), None)
    if not target:
        raise ValueError(f"Matched item with line index {line_index} not found.")

    if action in ["MARK_UNMATCHED", "REJECT"]:
        target.match_status = MatchStatus.UNMATCHED
        target.rfq_match = None
        target.item_master_match = None
        target.review_reasons = ["Marked as unmatched / non-catalog by procurement reviewer"]
    elif action in ["CLEAR", "RESET", "REOPEN"]:
        target.match_status = MatchStatus.REVIEW_REQUIRED
        target.rfq_match = None
        target.item_master_match = None
        target.review_reasons = ["Match reopened by procurement reviewer for re-evaluation"]
    elif action in ["ACCEPT", "CHOOSE", "MANUAL_OVERRIDE"]:
        if chosen_candidate_sku:
            item_master = get_item_master()
            master_rec = next((im for im in item_master if im.internal_sku.upper().strip() == chosen_candidate_sku.upper().strip()), None)
            if master_rec:
                cand = MatchCandidate(
                    candidate_item_id=master_rec.internal_item_id,
                    candidate_sku=master_rec.internal_sku,
                    candidate_description=master_rec.canonical_description,
                    match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                    match_score=1.0,
                    match_status=MatchStatus.EXACT_MATCH,
                    explanations=["Confirmed by procurement reviewer"],
                    matched_fields=["human_confirmation"],
                    source_provenance=target.quote_item.provenance
                )
                target.item_master_match = cand
                
                # Check UOM compatibility deterministically
                uom_res = target.uom_conversion
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
                    target.review_reasons = [uom_res.error_reason or "Incompatible UOM conversion"]
                else:
                    target.match_status = MatchStatus.EXACT_MATCH
                    target.review_reasons = []

                meta = get_quote_metadata(quote_id) or {}
                rfq_id = meta.get("rfq_id")
                rfq = get_rfq(rfq_id) if rfq_id else None
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
                                explanations=["Confirmed by procurement reviewer"],
                                matched_fields=["human_confirmation"],
                                source_provenance=target.quote_item.provenance
                            )
                            break

    quote_dir = DATA_DIR / quote_id
    with open(quote_dir / "matching" / "matched_items.json", "w", encoding="utf-8") as f:
        f.write(json.dumps([m.model_dump(mode="json") for m in matched_items], indent=2))

    meta = get_quote_metadata(quote_id) or {}
    has_review = any(m.match_status in [MatchStatus.REVIEW_REQUIRED, MatchStatus.UOM_INCOMPATIBLE] for m in matched_items)
    meta["match_status"] = "REVIEW_REQUIRED" if has_review else "MATCHED"
    with open(quote_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return matched_items


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
            review_rfq_line_ids = set()
            matched_quote_indices = set()

            for rfq_line in rfq_items:
                for m in matched:
                    if is_quote_item_matching_rfq_line(m, rfq_line):
                        matched_rfq_line_ids.add(rfq_line.rfq_line_id)
                        matched_quote_indices.add(m.quote_item.line_index)
                    elif is_quote_item_review_for_rfq_line(m, rfq_line):
                        review_rfq_line_ids.add(rfq_line.rfq_line_id)

            rfq_items_matched = len(matched_rfq_line_ids)
            extra_lines_count = max(0, lines_extracted_count - len(matched_quote_indices))
            unmatched_rfq_items_count = max(0, total_rfq_items - rfq_items_matched)
            review_count = len(review_rfq_line_ids) or sum(1 for m in matched if m.match_status in [MatchStatus.REVIEW_REQUIRED, MatchStatus.UOM_INCOMPATIBLE])

            pct_val, pct_str = format_coverage_pct(rfq_items_matched, total_rfq_items)

            q["rfq_items_matched"] = rfq_items_matched
            q["total_rfq_items"] = total_rfq_items
            q["lines_extracted_count"] = lines_extracted_count
            q["extra_lines_count"] = extra_lines_count
            q["unmatched_rfq_items_count"] = unmatched_rfq_items_count
            q["matched_exact"] = rfq_items_matched
            q["review_count"] = review_count
            q["unmatched_count"] = unmatched_rfq_items_count
            q["comparable_items_count"] = rfq_items_matched
            
            q["scope_coverage_pct"] = pct_val
            q["coverage_pct_str"] = pct_str
            q["scope_coverage_str"] = f"{rfq_items_matched} / {total_rfq_items} ({pct_str})"
            q["scope_breakdown_str"] = f"{lines_extracted_count} supplier lines extracted · {rfq_items_matched} matched to RFQ · {extra_lines_count} unmatched / extra"
            
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
                q["processing_status_label"] = f"Review Needed ({review_count})"
                q["processing_status_badge"] = "badge-warning"
                q["coverage_state"] = "REVIEW_REQUIRED"
                q["eligibility_status"] = "REVIEW_REQUIRED"
                q["whole_rfq_status"] = f"Review Needed ({review_count})"
                q["is_eligible"] = False
            elif rfq_items_matched > 0:
                q["processing_status"] = "READY_FOR_COMPARISON"
                q["processing_status_label"] = "Ready for Comparison"
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
    for f in sorted(COMPARISONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                ranking_rep = data.get("ranking_report") or {}
                l1_supp = ranking_rep.get("l1_supplier") or {}
                l1_name = l1_supp.get("supplier_name", "Split Optimal") if isinstance(l1_supp, dict) else "Split Optimal"
                l1_cost = l1_supp.get("total_landed_cost_base", "—") if isinstance(l1_supp, dict) else "—"
                comp_obj = data.get("comparison") or {}
                comparisons.append({
                    "comparison_id": data.get("comparison_id", f.stem),
                    "rfq_id": data.get("rfq_id"),
                    "title": data.get("title", f"Comparison for {data.get('rfq_id')}"),
                    "created_at": data.get("created_at"),
                    "supplier_count": len(comp_obj.get("suppliers", {})),
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

    comparison_id = f"COMP-{rfq_id}-{int(time.time())}"
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
        quote_id = q["test_id"]
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
    ensure_default_rfqs()
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


ensure_demo_workspace_initialized()


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


def _generate_fresh_award_proposal(rfq_id: str, comp: Dict[str, Any], scenario: str = "SINGLE_SUPPLIER_L1") -> Dict[str, Any]:
    ranking_report = comp.get("ranking_report") or {}
    comp_details = comp.get("comparison") or comp
    item_comps_raw = comp_details.get("item_comparisons", {})
    if isinstance(item_comps_raw, dict):
        item_comps = list(item_comps_raw.values())
    else:
        item_comps = list(item_comps_raw)

    base_cur = comp.get("base_currency", "INR")
    l1_supplier = ranking_report.get("l1_supplier")
    l1_supplier_id = l1_supplier.get("supplier_id") if l1_supplier and l1_supplier.get("is_eligible") else None

    allocations = []
    total_awarded_val = Decimal("0")
    fully_allocated_count = 0
    partially_allocated_count = 0
    unallocated_count = 0

    for item_comp in item_comps:
        rfq_line_id = item_comp.get("rfq_line_id")
        sku = item_comp.get("sku") or item_comp.get("rfq_line_id")
        desc = item_comp.get("item_description") or item_comp.get("description") or sku
        req_qty = Decimal(str(item_comp.get("requested_quantity", 100)))
        req_uom = item_comp.get("requested_uom", "PCS")

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
            landed = Decimal(str(supp_price.get("unit_landed_price_base") or supp_price.get("effective_unit_landed_cost") or 0))
            cur = supp_price.get("quoted_currency") or supp_price.get("currency", base_cur)
            qqty = Decimal(str(supp_price.get("quoted_qty") or supp_price.get("quoted_quantity", req_qty)))
            quom = supp_price.get("quoted_uom", req_uom)
            is_comparable = supp_price.get("is_comparable", True)

            if landed > 0 and is_comparable and (min_line_cost is None or landed < min_line_cost):
                min_line_cost = landed
                l1_line_supplier_id = sid

            if is_comparable and landed > 0:
                supplier_bids.append({
                    "supplier_id": sid,
                    "supplier_name": sname,
                    "quote_id": qid,
                    "quoted_qty": str(qqty),
                    "quoted_uom": quom,
                    "unit_landed_cost": str(landed),
                    "currency": cur,
                    "is_comparable": True,
                    "is_l1_for_line": False,
                })

        for b in supplier_bids:
            if b["supplier_id"] == l1_line_supplier_id:
                b["is_l1_for_line"] = True

        splits = []
        selected_bid = None

        if scenario == "SINGLE_SUPPLIER_L1" and l1_supplier_id:
            for b in supplier_bids:
                if b["supplier_id"] == l1_supplier_id:
                    selected_bid = b
                    break
        elif scenario == "LINE_ITEM_OPTIMAL" and l1_line_supplier_id:
            for b in supplier_bids:
                if b["supplier_id"] == l1_line_supplier_id:
                    selected_bid = b
                    break

        if not selected_bid and supplier_bids:
            selected_bid = supplier_bids[0]

        if selected_bid:
            unit_price = Decimal(selected_bid["unit_landed_cost"])
            awarded_qty = req_qty
            split_val = awarded_qty * unit_price
            splits.append({
                "supplier_id": selected_bid["supplier_id"],
                "supplier_name": selected_bid["supplier_name"],
                "quote_id": selected_bid["quote_id"],
                "allocated_qty": str(awarded_qty),
                "quoted_uom": selected_bid["quoted_uom"],
                "unit_landed_cost": str(unit_price),
                "split_value": str(split_val),
                "is_l1_for_line": selected_bid.get("is_l1_for_line", False),
                "quoted_capacity": str(selected_bid["quoted_qty"])
            })
            total_allocated = awarded_qty
            unallocated = Decimal("0")
            line_val = split_val
            total_awarded_val += line_val
            fully_allocated_count += 1
            alloc_state = "FULLY_ALLOCATED"
        else:
            total_allocated = Decimal("0")
            unallocated = req_qty
            line_val = Decimal("0")
            unallocated_count += 1
            alloc_state = "NOT_QUOTED" if not supplier_bids else "UNALLOCATED"

        allocations.append({
            "rfq_line_id": rfq_line_id,
            "item_sku": sku,
            "item_description": desc,
            "required_qty": str(req_qty),
            "required_uom": req_uom,
            "total_allocated_qty": str(total_allocated),
            "unallocated_qty": str(unallocated),
            "total_line_value": str(line_val),
            "allocation_state": alloc_state,
            "supplier_splits": splits,
            "available_bids": supplier_bids
        })

    has_unalloc = unallocated_count > 0 or partially_allocated_count > 0 or any(Decimal(a["unallocated_qty"]) > 0 for a in allocations)

    return {
        "award_id": f"AWD-{rfq_id}",
        "rfq_id": rfq_id,
        "comparison_id": comp["comparison_id"],
        "status": "DRAFT",
        "selected_scenario": scenario,
        "base_currency": base_cur,
        "total_awarded_value": str(total_awarded_val),
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
        default_scenario = existing_award.get("selected_scenario") or "SINGLE_SUPPLIER_L1"
        fresh_bids_proposal = _generate_fresh_award_proposal(rfq_id, comp, default_scenario)
        bids_by_line = {a["rfq_line_id"]: a.get("available_bids", []) for a in fresh_bids_proposal.get("allocations", [])}
        for a in existing_award.get("allocations", []):
            if "available_bids" not in a or not a["available_bids"]:
                a["available_bids"] = bids_by_line.get(a["rfq_line_id"], [])
        return existing_award

    # 2. REOPENED awards preserve saved allocations unless explicitly switching scenario
    if existing_award and existing_award.get("status") == "REOPENED" and not scenario:
        default_scenario = existing_award.get("selected_scenario") or "SINGLE_SUPPLIER_L1"
        fresh_bids_proposal = _generate_fresh_award_proposal(rfq_id, comp, default_scenario)
        bids_by_line = {a["rfq_line_id"]: a.get("available_bids", []) for a in fresh_bids_proposal.get("allocations", [])}
        for a in existing_award.get("allocations", []):
            if "available_bids" not in a or not a["available_bids"]:
                a["available_bids"] = bids_by_line.get(a["rfq_line_id"], [])
        return existing_award

    # 3. Draft mode or explicit scenario switch
    target_scenario = scenario or (existing_award.get("selected_scenario") if existing_award else "SINGLE_SUPPLIER_L1")
    proposal = _generate_fresh_award_proposal(rfq_id, comp, target_scenario)

    if existing_award:
        proposal["award_id"] = existing_award.get("award_id", f"AWD-{rfq_id}")
        proposal["status"] = existing_award.get("status", "DRAFT")
        proposal["buyer_accepted_unallocated"] = existing_award.get("buyer_accepted_unallocated", False)

    return proposal


def save_award_decision(rfq_id: str, award_data: Dict[str, Any], is_finalized: bool = False) -> Dict[str, Any]:
    """
    Persists an award decision with strict server-side validation gates:
    1. RFQ and Comparison ownership verification.
    2. Finalized award immutability (cannot re-finalize without explicit reopening).
    3. Strict Decimal non-negative quantity parsing.
    4. Authoritative unit price resolution from comparison (never trusts client price).
    5. Supplier eligibility check against authoritative comparison bids.
    6. Partial/unallocated award acknowledgement enforcement.
    7. Deterministic Decimal financial recalculation.
    """
    rfq = get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    comp = get_latest_rfq_comparison(rfq_id)
    if not comp:
        raise ValueError(f"No valid commercial comparison available for RFQ {rfq_id}.")
    comp_id = comp.get("comparison_id", "COMP-UNKNOWN")

    existing_award = get_award_decision(rfq_id)
    val_errors: List[str] = []
    val_warnings: List[str] = []

    # Safeguard 1: Finalized Immutability Guard
    if existing_award and existing_award.get("status") == "FINALIZED" and is_finalized:
        # A direct finalize request on an already finalized award must be rejected
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
        req_qty = Decimal(str(canonical_line.get("required_qty", 0)))
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

            authoritative_unit_landed_cost = Decimal(str(bid["unit_landed_cost"]))
            
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

            # Safeguard 6: Deterministic Decimal Financial Calculation
            sp_val = quantize_currency(sp_qty * authoritative_unit_landed_cost)
            line_alloc_qty += sp_qty
            line_total_val += sp_val

            sanitized_splits.append({
                "supplier_id": bid["supplier_id"],
                "supplier_name": bid["supplier_name"],
                "quote_id": bid["quote_id"],
                "allocated_qty": str(sp_qty),
                "quoted_uom": bid.get("quoted_uom", req_uom),
                "unit_landed_cost": str(authoritative_unit_landed_cost),
                "split_value": str(sp_val),
                "is_l1_for_line": bid.get("is_l1_for_line", False),
                "quoted_capacity": str(bid.get("quoted_qty", req_qty))
            })

        # Safeguard 7: Over-Allocation Check
        if line_alloc_qty > req_qty:
            val_errors.append(f"Line {line_id} ({item_sku}): Total allocated quantity ({line_alloc_qty}) exceeds required quantity ({req_qty}).")
            alloc_state = "OVER_ALLOCATED"
        elif line_alloc_qty == req_qty and req_qty > Decimal("0"):
            fully_alloc_count += 1
            alloc_state = "FULLY_ALLOCATED"
        elif line_alloc_qty > Decimal("0"):
            partially_alloc_count += 1
            alloc_state = "PARTIALLY_ALLOCATED"
            val_warnings.append(f"Line {line_id}: Partially allocated ({line_alloc_qty}/{req_qty} {req_uom}).")
        else:
            unalloc_count += 1
            alloc_state = "UNALLOCATED"
            val_warnings.append(f"Line {line_id} ({item_sku}): 0 quantity allocated.")

        unalloc_qty = max(Decimal("0"), req_qty - line_alloc_qty)
        total_val += line_total_val

        sanitized_allocations.append({
            "rfq_line_id": line_id,
            "item_sku": item_sku,
            "item_description": item_desc,
            "required_qty": str(req_qty),
            "required_uom": req_uom,
            "total_allocated_qty": str(line_alloc_qty),
            "unallocated_qty": str(unalloc_qty),
            "total_line_value": str(line_total_val),
            "allocation_state": alloc_state,
            "supplier_splits": sanitized_splits,
            "available_bids": canonical_line.get("available_bids", [])
        })

    # Include any missing RFQ lines as unallocated
    for line_id, canonical_line in rfq_lines_map.items():
        if line_id not in seen_line_ids:
            req_qty = Decimal(str(canonical_line.get("required_qty", 0)))
            unalloc_count += 1
            sanitized_allocations.append({
                "rfq_line_id": line_id,
                "item_sku": canonical_line.get("item_sku", line_id),
                "item_description": canonical_line.get("item_description", ""),
                "required_qty": str(req_qty),
                "required_uom": canonical_line.get("required_uom", "PCS"),
                "total_allocated_qty": "0",
                "unallocated_qty": str(req_qty),
                "total_line_value": "0.00",
                "allocation_state": "UNALLOCATED",
                "supplier_splits": [],
                "available_bids": canonical_line.get("available_bids", [])
            })
            val_warnings.append(f"Line {line_id}: Not included in submission (marked unallocated).")

    # Safeguard 8: Partial / Unallocated Award Acknowledgement Requirement
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
    Cleans all user RFQs, comparisons, awards, and quotes back to a fresh state.
    Restores the standard Item Master catalog.
    """
    # 1. Clean RFQs
    if RFQS_DIR.exists():
        shutil.rmtree(RFQS_DIR)
    RFQS_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Clean Comparisons
    if COMPARISONS_DIR.exists():
        shutil.rmtree(COMPARISONS_DIR)
    COMPARISONS_DIR.mkdir(parents=True, exist_ok=True)

    # 3. Clean Awards
    if AWARDS_DIR.exists():
        shutil.rmtree(AWARDS_DIR)
    AWARDS_DIR.mkdir(parents=True, exist_ok=True)

    # 4. Clean quotes & test directories
    for item in list(DATA_DIR.iterdir()):
        if item.name in ["item_master", "rfqs", "comparisons", "awards", "settings.json"]:
            continue
        if item.is_dir():
            try:
                shutil.rmtree(item)
            except Exception:
                pass
        elif item.is_file():
            try:
                item.unlink()
            except Exception:
                pass

    # 5. Restore baseline Item Master
    ITEM_MASTER_DIR.mkdir(parents=True, exist_ok=True)
    im_file = ITEM_MASTER_DIR / "items.json"
    benchmark_im = BASE_DIR / "datasets" / "procurement_benchmark" / "item_master.json"
    if benchmark_im.exists():
        im_file.write_text(benchmark_im.read_text(encoding="utf-8"), encoding="utf-8")

    return {"status": "success", "message": "All procurement data reset to clean initial state."}


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
