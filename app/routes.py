"""
FastAPI Routes for Quote Intelligence Enterprise Web Application.
Exposes procurement workflows across RFQs, Quotation Ingestion, Item Master, Comparisons, Review Center, and Developer Lab.
"""

from decimal import Decimal
import json
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import services
from core.currency import CurrencyRateService
from comparison.models import ChargeAllocationMethod
from matching.matcher import ItemMatcher
from matching.models import ItemMasterRecord
from core.canonical_quote import QuoteItem


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# =========================================================================
# 1. PROCUREMENT ROUNDS / RFQS HUB & WORKSPACE
# =========================================================================
@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    all_rfqs = services.list_rfqs(include_demo=False)
    if not all_rfqs:
        all_rfqs = services.list_rfqs(include_demo=True)
    recent_rfqs = all_rfqs[:5]
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "recent_rfqs": recent_rfqs,
            "total_rfqs_count": len(all_rfqs),
            "active_tab": "home"
        }
    )


@router.get("/rfqs", response_class=HTMLResponse)
async def list_rfqs_page(request: Request):
    rfqs = services.list_rfqs()
    return templates.TemplateResponse(
        request=request,
        name="rfqs/list.html",
        context={"rfqs": rfqs, "active_tab": "rfqs"}
    )


@router.get("/rfqs/create", response_class=HTMLResponse)
async def create_rfq_page(request: Request):
    item_master = services.get_item_master()
    return templates.TemplateResponse(
        request=request,
        name="rfqs/create.html",
        context={"item_master": item_master, "active_tab": "rfqs"}
    )


@router.post("/rfqs")
async def save_new_rfq(
    rfq_id: str = Form(...),
    title: str = Form(...),
    base_currency: str = Form("INR"),
    items_json: str = Form("[]")
):
    try:
        items = json.loads(items_json)
    except Exception:
        items = []

    services.create_rfq(rfq_id, title, base_currency, items)
    return RedirectResponse(url=f"/rfqs/{rfq_id}", status_code=303)


@router.get("/rfqs/{rfq_id}", response_class=HTMLResponse)
@router.get("/workspace", response_class=HTMLResponse)
async def view_rfq_workspace(request: Request, rfq_id: Optional[str] = None):
    target_rfq_id = rfq_id or services.DEMO_RFQ_ID
    data = services.get_rfq_workspace_data(target_rfq_id)
    if not data or not data.get("rfq"):
        raise HTTPException(status_code=404, detail="RFQ not found")

    return templates.TemplateResponse(
        request=request,
        name="rfqs/detail.html",
        context={
            **data,
            "active_tab": "rfqs"
        }
    )



@router.post("/rfqs/{rfq_id}/upload")
@router.post("/rfqs/{rfq_id}/upload-quote")
@router.post("/rfqs/{rfq_id}/upload-quotes")
async def upload_quote_to_rfq(request: Request, rfq_id: str):
    form = await request.form()
    
    # Retrieve all files posted under 'files', 'file', or any multi-item file field
    uploaded_files = []
    for k, v in form.multi_items():
        if hasattr(v, "filename") and v.filename:
            uploaded_files.append(v)

    supplier_names = form.getlist("supplier_names")
    supplier_name_single = form.get("supplier_name")

    if not uploaded_files:
        accept_header = request.headers.get("accept", "")
        if "application/json" in accept_header:
            return JSONResponse(status_code=400, content={"status": "error", "message": "No files selected."})
        return RedirectResponse(url=f"/rfqs/{rfq_id}", status_code=303)

    results = []
    failed = []

    for idx, f in enumerate(uploaded_files):
        try:
            content = await f.read()
            if not content or len(content) == 0:
                failed.append({"filename": f.filename, "error": "File is empty (0 bytes)"})
                continue

            s_name = None
            if supplier_names and idx < len(supplier_names) and supplier_names[idx] and str(supplier_names[idx]).strip():
                s_name = str(supplier_names[idx]).strip()
            elif supplier_name_single and idx == 0 and str(supplier_name_single).strip():
                s_name = str(supplier_name_single).strip()

            quote_id = services.ingest_quote_file_for_rfq(rfq_id, f.filename, content, s_name)
            results.append({"filename": f.filename, "quote_id": quote_id, "status": "SUCCESS"})
        except Exception as exc:
            failed.append({"filename": f.filename, "error": str(exc)})

    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header:
        return JSONResponse(content={
            "status": "success" if results else "error",
            "processed_count": len(results),
            "failed_count": len(failed),
            "results": results,
            "failed": failed
        })

    return RedirectResponse(url=f"/rfqs/{rfq_id}", status_code=303)


# =========================================================================
# 2. CANONICAL ITEM MASTER CATALOG (COMPANY PRODUCT DATABASE)
# =========================================================================
# =========================================================================
# 2. CANONICAL ITEM MASTER CATALOG (COMPANY PRODUCT DATABASE)
# =========================================================================
@router.get("/item-master", response_class=HTMLResponse)
async def item_master_page(request: Request):
    items = services.get_item_master(include_inactive=True)
    brands = sorted(list({it.brand for it in items if it.brand}))
    uoms = sorted(list({it.stocking_uom for it in items if it.stocking_uom}))
    batches = services.get_import_batches()
    active_count = sum(1 for it in items if it.status != "INACTIVE")
    inactive_count = sum(1 for it in items if it.status == "INACTIVE")
    return templates.TemplateResponse(
        request=request,
        name="item_master/index.html",
        context={
            "items": items,
            "brands": brands,
            "uoms": uoms,
            "batches": batches,
            "total_count": len(items),
            "active_count": active_count,
            "inactive_count": inactive_count,
            "active_tab": "item_master"
        }
    )


@router.post("/api/item-master/{item_id}/deactivate")
async def api_deactivate_item_master(item_id: str):
    success, msg = services.deactivate_item_master_record(item_id)
    if not success:
        return JSONResponse(status_code=400, content={"status": "error", "message": msg})
    return JSONResponse(content={"status": "success", "message": msg})


@router.post("/api/item-master/{item_id}/restore")
async def api_restore_item_master(item_id: str):
    success, msg = services.restore_item_master_record(item_id)
    if not success:
        return JSONResponse(status_code=400, content={"status": "error", "message": msg})
    return JSONResponse(content={"status": "success", "message": msg})


@router.post("/api/item-master/{item_id}/delete")
async def api_delete_item_master(item_id: str):
    success, msg = services.delete_item_master_record(item_id)
    if not success:
        return JSONResponse(status_code=400, content={"status": "error", "message": msg})
    return JSONResponse(content={"status": "success", "message": msg})


@router.post("/api/item-master/bulk-action")
async def api_bulk_action_item_master(request: Request):
    data = await request.json()
    action = data.get("action")
    item_ids = data.get("item_ids", [])
    if not item_ids:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No items selected."})

    if action == "deactivate":
        count = services.bulk_deactivate_items(item_ids)
        return JSONResponse(content={"status": "success", "message": f"Deactivated {count} items.", "count": count})
    elif action == "restore":
        count = services.bulk_restore_items(item_ids)
        return JSONResponse(content={"status": "success", "message": f"Restored {count} items to Active.", "count": count})
    elif action == "delete":
        res = services.bulk_delete_items(item_ids)
        return JSONResponse(content={"status": "success", "deleted_count": res["deleted_count"], "blocked_count": res["blocked_count"], "blocked_items": res["blocked_items"]})
    else:
        return JSONResponse(status_code=400, content={"status": "error", "message": f"Unknown action '{action}'."})


@router.get("/api/item-master/import-history")
async def api_get_import_history():
    batches = services.get_import_batches()
    return JSONResponse(content={"status": "success", "batches": batches})


@router.get("/api/item-master/import-history/{batch_id}/preview-undo")
async def api_preview_undo_import_batch(batch_id: str):
    preview = services.preview_undo_import_batch(batch_id)
    return JSONResponse(content={"status": "success", "preview": preview})


@router.post("/api/item-master/import-history/{batch_id}/undo")
async def api_undo_import_batch(batch_id: str):
    result = services.undo_import_batch(batch_id)
    return JSONResponse(content={"status": "success", "result": result})


@router.get("/item-master/import", response_class=HTMLResponse)
async def import_item_master_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="item_master/import.html",
        context={"active_tab": "item_master"}
    )


@router.post("/item-master/import/preview", response_class=HTMLResponse)
async def preview_item_master_import(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    extraction_result = services.parse_catalog_upload(file.filename, content)
    return templates.TemplateResponse(
        request=request,
        name="item_master/import_preview.html",
        context={
            "result": extraction_result,
            "preview_items": extraction_result.raw_preview_data,
            "filename": file.filename,
            "count": extraction_result.valid_products_count,
            "preview_json": json.dumps(extraction_result.raw_preview_data),
            "active_tab": "item_master"
        }
    )


@router.post("/item-master/import/confirm")
async def confirm_item_master_import(
    items_json: str = Form(...),
    mode: str = Form("append")
):
    try:
        items_data = json.loads(items_json)
        services.commit_catalog_import(items_data, mode=mode)
    except Exception:
        pass
    return RedirectResponse(url="/item-master", status_code=303)


@router.get("/item-master/new", response_class=HTMLResponse)
async def create_item_master_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="item_master/create.html",
        context={"active_tab": "item_master"}
    )


@router.post("/item-master/new")
async def create_item_master_record(
    internal_sku: str = Form(...),
    canonical_description: str = Form(...),
    stocking_uom: str = Form("PCS"),
    manufacturer_part_number: Optional[str] = Form(None),
    brand: Optional[str] = Form(None),
    supplier_parts: Optional[str] = Form(None),
    specifications_raw: Optional[str] = Form(None)
):
    approved_pns = [p.strip() for p in (supplier_parts or "").split(",") if p.strip()]
    specs = {}
    if specifications_raw:
        for line in specifications_raw.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                specs[k.strip()] = v.strip()
    
    services.add_item_to_master(
        internal_sku=internal_sku,
        canonical_description=canonical_description,
        stocking_uom=stocking_uom,
        manufacturer_part_number=manufacturer_part_number,
        brand=brand,
        approved_supplier_part_numbers=approved_pns,
        specifications=specs
    )
    return RedirectResponse(url="/item-master", status_code=303)


@router.get("/item-master/{item_id}", response_class=HTMLResponse)
async def view_item_master_detail(request: Request, item_id: str):
    item = services.get_item_master_item(item_id)
    if not item:
        return RedirectResponse(url="/item-master", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="item_master/detail.html",
        context={"item": item, "active_tab": "item_master"}
    )


# =========================================================================
# 3. SUPPLIER QUOTATION REVIEW & EXTRACTION
# =========================================================================
@router.get("/quotes", response_class=HTMLResponse)
async def list_quotes_page(request: Request):
    quotes = services.list_quotes()
    rfqs = services.list_rfqs()
    return templates.TemplateResponse(
        request=request,
        name="quotes/list.html",
        context={"quotes": quotes, "rfqs": rfqs, "active_tab": "rfqs"}
    )


@router.get("/quotes/upload", response_class=HTMLResponse)
async def upload_quote_page(request: Request):
    rfqs = services.list_rfqs()
    return templates.TemplateResponse(
        request=request,
        name="quotes/upload.html",
        context={"rfqs": rfqs, "active_tab": "rfqs"}
    )


@router.post("/quotes")
async def create_quote_entry(
    quote_name: Optional[str] = Form(None),
    rfq_id: Optional[str] = Form(None)
):
    name = quote_name or "New Supplier Quote"
    quote_id = services.create_quote(name, rfq_id)
    return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)


@router.post("/tests")
async def create_test_entry(
    test_name: Optional[str] = Form(None),
    rfq_id: Optional[str] = Form(None)
):
    name = test_name or "New Developer Test"
    test_id = services.create_test(name, rfq_id)
    return RedirectResponse(url=f"/tests/{test_id}", status_code=303)



@router.get("/quotes/{quote_id}", response_class=HTMLResponse)
@router.get("/rfqs/{rfq_id}/quotes/{quote_id}", response_class=HTMLResponse)
@router.get("/tests/{quote_id}", response_class=HTMLResponse)
async def view_quote_detail(request: Request, quote_id: str, rfq_id: Optional[str] = None):
    data = services.get_quote_details(quote_id)
    if not data:
        raise HTTPException(status_code=404, detail="Quote not found")

    resolved_rfq_id = rfq_id or data["metadata"].get("rfq_id")
    rfq = services.get_rfq(resolved_rfq_id) if resolved_rfq_id else None
    rfqs = services.list_rfqs()
    rfq_quote_data = None
    if resolved_rfq_id:
        rfq_quotes = services.get_quotes_for_rfq(resolved_rfq_id)
        rfq_quote_data = next((q for q in rfq_quotes if q["test_id"] == quote_id), None)

    return templates.TemplateResponse(
        request=request,
        name="quotes/detail.html",
        context={
            "quote_meta": data["metadata"],
            "quote": data["canonical_quote"],
            "report": data["report"],
            "has_source": data["has_source"],
            "sheets": data["sheets"],
            "sheet_data": data["sheet_data"],
            "rfq": rfq,
            "rfq_id": resolved_rfq_id,
            "rfqs": rfqs,
            "rfq_quote_data": rfq_quote_data,
            "active_tab": "rfqs"
        }
    )


@router.post("/quotes/{quote_id}/upload")
@router.post("/tests/{quote_id}/upload")
async def upload_quote_document(quote_id: str, file: UploadFile = File(...)):
    content = await file.read()
    services.upload_source_file(quote_id, file.filename, content)
    return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)


@router.post("/quotes/{quote_id}/extract")
@router.post("/tests/{quote_id}/process")
async def run_quote_extraction(quote_id: str):
    try:
        services.run_extraction(quote_id)
    except Exception:
        pass
    return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)


@router.post("/quotes/{quote_id}/correct")
@router.post("/tests/{quote_id}/correct")
async def apply_correction_endpoint(
    quote_id: str,
    line_index: int = Form(...),
    field_name: str = Form(...),
    new_value: str = Form(...)
):
    services.apply_field_correction(quote_id, line_index, field_name, new_value)
    return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)


@router.get("/quotes/{quote_id}/source")
@router.get("/tests/{quote_id}/source")
async def view_source_inline(quote_id: str):
    meta = services.get_quote_metadata(quote_id)
    if not meta or not meta.get("source_file_name"):
        raise HTTPException(status_code=404, detail="Source file not found")

    source_path = services.DATA_DIR / quote_id / "source" / meta["source_file_name"]
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="File missing from disk")

    file_name = meta["source_file_name"]
    media_type = "application/octet-stream"
    if file_name.lower().endswith(".pdf"):
        media_type = "application/pdf"
    elif file_name.lower().endswith(".xlsx"):
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif file_name.lower().endswith(".csv"):
        media_type = "text/csv"

    return FileResponse(
        str(source_path),
        media_type=media_type,
        filename=file_name if file_name.lower().endswith(".pdf") else None,
        content_disposition_type="inline"
    )


@router.get("/quotes/{quote_id}/download")
@router.get("/tests/{quote_id}/download")
async def download_source_file(quote_id: str):
    meta = services.get_quote_metadata(quote_id)
    if not meta or not meta.get("source_file_name"):
        raise HTTPException(status_code=404, detail="Source file not found")

    source_path = services.DATA_DIR / quote_id / "source" / meta["source_file_name"]
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")

    return FileResponse(
        str(source_path),
        filename=meta["source_file_name"],
        content_disposition_type="attachment"
    )


@router.post("/quotes/{quote_id}/reset")
@router.post("/tests/{quote_id}/reset")
async def reset_quote_output(quote_id: str):
    services.reset_test(quote_id)
    return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)


@router.post("/quotes/{quote_id}/rename")
@router.post("/tests/{quote_id}/rename")
async def rename_quote(quote_id: str, new_name: str = Form(...)):
    services.rename_test(quote_id, new_name)
    return RedirectResponse(url=f"/quotes/{quote_id}", status_code=303)


@router.post("/quotes/{quote_id}/delete")
@router.post("/tests/{quote_id}/delete")
async def delete_quote(quote_id: str):
    services.delete_test(quote_id)
    return RedirectResponse(url="/rfqs", status_code=303)


@router.get("/quotes/{quote_id}/json")
@router.get("/tests/{quote_id}/json")
async def get_quote_json(quote_id: str):
    quote = services.get_canonical_quote(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail="Extraction output not found")
    return Response(content=quote.model_dump_json(indent=2), media_type="application/json")


# =========================================================================
# 4. ITEM MASTER MATCHING & CANDIDATE RESOLUTION
# =========================================================================
@router.get("/quotes/{quote_id}/match", response_class=HTMLResponse)
@router.get("/rfqs/{rfq_id}/quotes/{quote_id}/match", response_class=HTMLResponse)
async def view_matching_page(request: Request, quote_id: str, rfq_id: Optional[str] = None):
    quote = services.get_canonical_quote(quote_id)
    if not quote:
        raise HTTPException(status_code=400, detail="Quote not extracted yet. Run extraction first.")

    meta = services.get_quote_metadata(quote_id) or {}
    resolved_rfq_id = rfq_id or meta.get("rfq_id")

    matched_items = services.get_matched_items(quote_id)
    if not matched_items:
        matched_items = services.run_matching_for_quote(quote_id, resolved_rfq_id)

    rfq = services.get_rfq(resolved_rfq_id) if resolved_rfq_id else None
    rfqs = services.list_rfqs()
    item_master = services.get_item_master()

    return templates.TemplateResponse(
        request=request,
        name="quotes/match.html",
        context={
            "quote_id": quote_id,
            "quote": quote,
            "quote_meta": meta,
            "matched_items": matched_items,
            "rfq": rfq,
            "rfq_id": resolved_rfq_id,
            "rfqs": rfqs,
            "item_master": item_master,
            "active_tab": "rfqs"
        }
    )


@router.post("/quotes/{quote_id}/match/run")
async def execute_matching_route(quote_id: str, rfq_id: Optional[str] = Form(None)):
    services.run_matching_for_quote(quote_id, rfq_id)
    return RedirectResponse(url=f"/quotes/{quote_id}/match", status_code=303)


@router.post("/quotes/{quote_id}/match/resolve")
async def resolve_match_route(
    request: Request,
    quote_id: str,
    line_index: int = Form(...),
    chosen_candidate_sku: Optional[str] = Form(None),
    action_: str = Form("ACCEPT")
):
    services.resolve_match_candidate(quote_id, line_index, chosen_candidate_sku, action_)
    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header:
        return JSONResponse(content={
            "status": "success",
            "quote_id": quote_id,
            "line_index": line_index,
            "action": action,
            "chosen_sku": chosen_candidate_sku
        })
    return RedirectResponse(url=f"/quotes/{quote_id}/match", status_code=303)


# =========================================================================
# 4B. DEDICATED RFQ-LEVEL BATCH REVIEW QUEUE
# =========================================================================
@router.get("/rfqs/{rfq_id}/review", response_class=HTMLResponse)
async def rfq_batch_review_page(request: Request, rfq_id: str):
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail=f"RFQ {rfq_id} not found")

    queue_data = services.get_rfq_review_queue(rfq_id)
    return templates.TemplateResponse(
        request=request,
        name="rfqs/review.html",
        context={
            "rfq": queue_data["rfq"],
            "rfq_id": rfq_id,
            "issues": queue_data["issues"],
            "stats": queue_data["stats"],
            "item_master": queue_data["item_master"],
            "active_tab": "rfqs"
        }
    )


@router.post("/rfqs/{rfq_id}/review/resolve")
async def rfq_batch_review_resolve(
    request: Request,
    rfq_id: str,
    quote_id: str = Form(...),
    line_index: int = Form(...),
    chosen_candidate_sku: Optional[str] = Form(None),
    action: str = Form("ACCEPT")
):
    services.resolve_match_candidate(quote_id, line_index, chosen_candidate_sku, action)
    new_queue = services.get_rfq_review_queue(rfq_id)
    
    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header:
        return JSONResponse(content={
            "status": "success",
            "issue_id": f"{quote_id}_{line_index}",
            "quote_id": quote_id,
            "line_index": line_index,
            "action": action,
            "chosen_sku": chosen_candidate_sku,
            "stats": new_queue["stats"]
        })
    return RedirectResponse(url=f"/rfqs/{rfq_id}/review", status_code=303)


@router.post("/rfqs/{rfq_id}/review/bulk-accept-safe")
async def rfq_batch_review_bulk_accept_safe(request: Request, rfq_id: str):
    result = services.bulk_resolve_safe_matches(rfq_id)
    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header:
        return JSONResponse(content=result)
    return RedirectResponse(url=f"/rfqs/{rfq_id}/review", status_code=303)



# =========================================================================
# 5. COMMERCIAL COMPARISONS & AWARD RECOMMENDATIONS
# =========================================================================
@router.get("/comparison", response_class=HTMLResponse)
async def default_comparison_redirect(request: Request):
    return RedirectResponse(url="/rfqs", status_code=303)


@router.get("/rfqs/{rfq_id}/comparison", response_class=HTMLResponse)
@router.get("/rfqs/{rfq_id}/compare", response_class=HTMLResponse)
async def rfq_comparison_shortcut(rfq_id: str):
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")
    comp = services.get_latest_rfq_comparison(rfq_id)
    if comp:
        return RedirectResponse(url=f"/comparisons/{comp['comparison_id']}", status_code=303)
    return RedirectResponse(url=f"/rfqs/{rfq_id}/comparison/setup", status_code=303)



@router.get("/comparisons", response_class=HTMLResponse)
async def list_comparisons_page(request: Request):
    comparisons = services.list_comparisons()
    return templates.TemplateResponse(
        request=request,
        name="comparisons/list.html",
        context={"comparisons": comparisons, "active_tab": "comparisons"}
    )


@router.get("/comparisons/create", response_class=HTMLResponse)
@router.get("/rfqs/{rfq_id}/comparison/setup", response_class=HTMLResponse)
@router.get("/rfqs/{rfq_id}/compare/setup", response_class=HTMLResponse)
async def create_comparison_page(request: Request, rfq_id: Optional[str] = None):
    all_rfqs = services.list_rfqs(include_demo=True)
    
    # Canonical RFQ resolution
    target_rfq_id = rfq_id
    if not target_rfq_id and all_rfqs:
        target_rfq_id = all_rfqs[0]["rfq_id"]

    selected_rfq = services.get_rfq(target_rfq_id) if target_rfq_id else None

    # Invariant: If selected_rfq exists, ensure it is present in rfqs list
    if selected_rfq and not any(r["rfq_id"] == selected_rfq.rfq_id for r in all_rfqs):
        all_rfqs.insert(0, {
            "rfq_id": selected_rfq.rfq_id,
            "title": selected_rfq.title,
            "base_currency": selected_rfq.base_currency,
            "line_item_count": len(selected_rfq.items),
            "quote_count": len(services.get_quotes_for_rfq(selected_rfq.rfq_id)),
            "is_demo": selected_rfq.rfq_id == services.DEMO_RFQ_ID
        })

    quotes = services.get_quotes_for_rfq(selected_rfq.rfq_id) if selected_rfq else []
    base_curr = (selected_rfq.base_currency if selected_rfq else "INR").upper().strip()
    
    # Automatically retrieve live rates for all quote currencies
    live_rates = {}
    for q in quotes:
        cur = (q.get("currency") or base_curr).upper().strip()
        if cur != base_curr and cur not in live_rates:
            rate_res = CurrencyRateService.get_exchange_rate(cur, base_curr)
            live_rates[cur] = {
                "rate": str(rate_res.rate),
                "date": rate_res.rate_date,
                "timestamp": rate_res.timestamp,
                "provider": rate_res.provider,
                "is_live": rate_res.is_live,
                "is_cached": rate_res.is_cached,
                "is_fallback": rate_res.is_fallback
            }

    return templates.TemplateResponse(
        request=request,
        name="comparisons/create.html",
        context={
            "rfqs": all_rfqs,
            "quotes": quotes,
            "selected_rfq": selected_rfq,
            "target_rfq_id": selected_rfq.rfq_id if selected_rfq else target_rfq_id,
            "live_rates": live_rates,
            "active_tab": "comparisons"
        }
    )


@router.post("/comparisons")
async def execute_comparison_route(
    rfq_id: str = Form(...),
    quote_ids: Optional[List[str]] = Form(None),
    base_currency: str = Form("INR"),
    charge_allocation: Optional[str] = Form("PROPORTIONAL_LINE_VALUE"),
    allocation_method: Optional[str] = Form(None)
):
    resolved_quote_ids = quote_ids or []
    if not resolved_quote_ids:
        quotes = services.get_quotes_for_rfq(rfq_id)
        if quotes:
            resolved_quote_ids = [q["test_id"] for q in quotes if q.get("is_eligible")] or [q["test_id"] for q in quotes]
    
    if not resolved_quote_ids:
        return RedirectResponse(url=f"/rfqs/{rfq_id}", status_code=303)

    alloc_str = allocation_method or charge_allocation or "PROPORTIONAL_LINE_VALUE"
    allocation_enum = ChargeAllocationMethod.PROPORTIONAL_LINE_VALUE
    if alloc_str == "PROPORTIONAL_QUANTITY":
        allocation_enum = ChargeAllocationMethod.PROPORTIONAL_QUANTITY
    elif alloc_str == "NONE":
        allocation_enum = ChargeAllocationMethod.NONE

    comp = services.run_rfq_comparison(
        rfq_id=rfq_id,
        quote_ids=resolved_quote_ids,
        base_currency=base_currency,
        charge_allocation_method=allocation_enum
    )
    return RedirectResponse(url=f"/comparisons/{comp['comparison_id']}", status_code=303)


@router.get("/comparisons/{comparison_id}", response_class=HTMLResponse)
async def view_comparison_detail(request: Request, comparison_id: str):
    comp_data = services.get_comparison(comparison_id)
    if not comp_data:
        raise HTTPException(status_code=404, detail="Comparison not found")

    rfq_id = comp_data.get("rfq_id")
    rfq = services.get_rfq(rfq_id) if rfq_id else None
    quotes = services.get_quotes_for_rfq(rfq_id) if rfq_id else []

    return templates.TemplateResponse(
        request=request,
        name="comparisons/detail.html",
        context={
            "comparison_id": comparison_id,
            "comparison": comp_data.get("comparison", {}),
            "ranking_report": comp_data.get("ranking_report", {}),
            "fx_metadata": comp_data.get("fx_metadata", {}),
            "rfq": rfq,
            "quotes": quotes,
            "active_tab": "comparisons"
        }
    )


# =========================================================================
# 6. ACTIONABLE REVIEW CENTER
# =========================================================================
@router.get("/review", response_class=HTMLResponse)
@router.get("/rfqs/{rfq_id}/review", response_class=HTMLResponse)
async def review_center_page(request: Request, rfq_id: Optional[str] = None):
    rfq = services.get_rfq(rfq_id) if rfq_id else None
    issues = services.get_review_center_issues(rfq_id)
    return templates.TemplateResponse(
        request=request,
        name="review/index.html",
        context={"issues": issues, "rfq": rfq, "rfq_id": rfq_id, "active_tab": "review"}
    )


# =========================================================================
# 7. CONTROLLED BENCHMARK DEMO SHOWCASE
# =========================================================================
@router.get("/demo", response_class=HTMLResponse)
async def demo_benchmark_showcase(request: Request):
    demo_data = services.get_demo_workspace_data()
    return templates.TemplateResponse(
        request=request,
        name="demo/index.html",
        context={
            "rfq": demo_data["rfq"],
            "quotes": demo_data["quotes"],
            "comparison": demo_data["comparison"],
            "ranking_report": demo_data["ranking_report"],
            "active_tab": "demo"
        }
    )


# =========================================================================
# 8. SEGREGATED DEVELOPER LAB & HISTORY
# =========================================================================
@router.get("/developer-lab", response_class=HTMLResponse)
@router.get("/history", response_class=HTMLResponse)
async def developer_lab_page(request: Request):
    all_quotes = services.list_quotes()
    dev_runs = [q for q in all_quotes if q["test_id"].startswith("TEST-")]
    return templates.TemplateResponse(
        request=request,
        name="developer_lab/index.html",
        context={
            "runs": dev_runs if dev_runs else all_quotes,
            "active_tab": "developer_lab"
        }
    )



# =========================================================================
# 9. PROGRAMMATIC API ENDPOINTS
# =========================================================================
@router.post("/api/quotes/{quote_id}/extract")
@router.get("/api/quotes/{quote_id}/extract")
async def api_extract_quote(quote_id: str):
    try:
        quote = services.run_extraction(quote_id)
        report = services.get_extraction_report(quote_id)
        return JSONResponse(content={
            "success": True,
            "quote_id": quote_id,
            "quote": json.loads(quote.model_dump_json()),
            "report": report
        })
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(exc)}
        )


@router.post("/rfqs/requirements/preview")
async def preview_rfq_requirements(file: UploadFile = File(...)):
    content = await file.read()
    extraction = services.parse_catalog_upload(file.filename, content)
    item_master = services.get_item_master()
    im_records = [
        ItemMasterRecord(
            internal_item_id=it.internal_item_id,
            internal_sku=it.internal_sku,
            manufacturer_part_number=it.manufacturer_part_number,
            approved_supplier_part_numbers=it.approved_supplier_part_numbers,
            canonical_description=it.canonical_description,
            stocking_uom=it.stocking_uom,
            approved_conversion_factors=it.approved_conversion_factors,
            brand=it.brand,
            specifications=it.specifications
        ) for it in item_master
    ]
    matcher = ItemMatcher()

    preview_rows = []
    for idx, item in enumerate(extraction.raw_preview_data):
        sku = item.get("sku") or item.get("internal_sku") or ""
        desc = item.get("canonical_description") or item.get("description") or ""
        mpn = item.get("manufacturer_part_number") or ""
        
        # Source-driven UOM: NEVER default to PCS if missing from source document
        raw_uom_in = item.get("requested_uom") or item.get("stocking_uom") or item.get("uom")
        uom = str(raw_uom_in).strip().upper() if raw_uom_in and str(raw_uom_in).strip().lower() not in ["none", "null", "n/a", "na", "-", "—"] else None
        
        # Source-driven Quantity: NEVER default to 100 or 1.0 if missing from source document
        raw_qty_in = item.get("requested_quantity") or item.get("quantity") or item.get("qty")
        qty_val: Optional[float] = None
        if raw_qty_in is not None and str(raw_qty_in).strip() not in ["", "None", "null", "N/A", "na", "-", "—"]:
            try:
                parsed_q = float(raw_qty_in)
                if parsed_q > 0:
                    qty_val = parsed_q
            except Exception:
                qty_val = None

        dummy_quote_item = QuoteItem(
            line_index=idx,
            raw_description=desc or sku,
            supplier_part_number=mpn or None,
            quoted_qty=Decimal(str(qty_val)) if qty_val is not None else Decimal("1.0"),
            quoted_uom=uom or "UNSPECIFIED",
            unit_price=Decimal("0.0")
        )
        matched_result = matcher.match_quote_item(dummy_quote_item, im_records)
        match_candidate = matched_result.item_master_match

        matched_sku = match_candidate.candidate_sku if match_candidate else (sku if any(im.internal_sku == sku for im in item_master) else None)
        matched_item_id = match_candidate.candidate_item_id if match_candidate else (next((im.internal_item_id for im in item_master if im.internal_sku == sku), None))
        match_status = matched_result.match_status.value

        is_complete = (qty_val is not None and uom is not None)
        missing_fields = []
        if qty_val is None:
            missing_fields.append("quantity")
        if uom is None:
            missing_fields.append("uom")

        preview_rows.append({
            "line_index": idx + 1,
            "raw_sku": sku,
            "raw_description": desc,
            "raw_uom": uom,
            "raw_qty": qty_val,
            "matched_item_id": matched_item_id,
            "matched_sku": matched_sku or sku,
            "matched_description": match_candidate.candidate_description if match_candidate else desc,
            "match_status": match_status,
            "match_score": round(match_candidate.match_score * 100, 1) if match_candidate else 0.0,
            "is_matched": matched_sku is not None,
            "is_complete": is_complete,
            "missing_fields": missing_fields
        })

    return JSONResponse(content={
        "filename": file.filename,
        "total_lines": len(preview_rows),
        "matched_count": sum(1 for r in preview_rows if r["is_matched"]),
        "complete_count": sum(1 for r in preview_rows if r["is_complete"]),
        "incomplete_count": sum(1 for r in preview_rows if not r["is_complete"]),
        "items": preview_rows
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    settings_data = services.get_application_settings()
    suppliers = services.get_supplier_master(include_inactive=True)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": settings_data,
            "suppliers": suppliers,
            "active_tab": "settings"
        }
    )


# =========================================================================
# SUPPLIER MASTER API ENDPOINTS
# =========================================================================
@router.get("/api/suppliers")
async def api_get_suppliers():
    suppliers = services.get_supplier_master(include_inactive=True)
    return JSONResponse(content={"status": "success", "suppliers": [s.model_dump(mode="json") for s in suppliers]})


@router.post("/api/suppliers/{supplier_id}/edit")
async def api_edit_supplier(supplier_id: str, request: Request):
    data = await request.json()
    new_name = data.get("supplier_name", "")
    success, msg, rec = services.update_supplier_name(supplier_id, new_name)
    if not success:
        return JSONResponse(status_code=400, content={"status": "error", "message": msg})
    return JSONResponse(content={"status": "success", "message": msg, "supplier": rec.model_dump(mode="json") if rec else None})


@router.post("/api/suppliers/{supplier_id}/deactivate")
async def api_deactivate_supplier(supplier_id: str):
    success, msg = services.deactivate_supplier(supplier_id)
    if not success:
        return JSONResponse(status_code=400, content={"status": "error", "message": msg})
    return JSONResponse(content={"status": "success", "message": msg})


@router.post("/api/suppliers/{supplier_id}/restore")
async def api_restore_supplier(supplier_id: str):
    success, msg = services.restore_supplier(supplier_id)
    if not success:
        return JSONResponse(status_code=400, content={"status": "error", "message": msg})
    return JSONResponse(content={"status": "success", "message": msg})


@router.post("/api/suppliers/{supplier_id}/delete")
async def api_delete_supplier(supplier_id: str):
    success, msg = services.delete_supplier(supplier_id)
    if not success:
        return JSONResponse(status_code=400, content={"status": "error", "message": msg})
    return JSONResponse(content={"status": "success", "message": msg})


@router.post("/settings")
@router.post("/api/settings")
async def save_settings_endpoint(request: Request):
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
        else:
            form_data = await request.form()
            payload = dict(form_data)
        
        updated = services.save_application_settings(payload)
        if "application/json" in content_type:
            return JSONResponse(content={"status": "success", "settings": updated})
        return RedirectResponse(url="/settings?saved=true", status_code=303)
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/api/settings/reset")
async def reset_data_endpoint():
    res = services.reset_all_procurement_data()
    return JSONResponse(content=res)


@router.post("/api/settings/seed-demo")
async def seed_demo_endpoint():
    res = services.seed_demo_benchmark_data()
    return JSONResponse(content=res)


@router.get("/api/settings/export")
async def export_data_endpoint():
    from fastapi.encoders import jsonable_encoder
    data = services.export_all_procurement_data()
    return JSONResponse(
        content=jsonable_encoder(data, custom_encoder={Decimal: str}),
        headers={"Content-Disposition": "attachment; filename=quote_intelligence_export.json"}
    )


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    data = services.get_analytics_data()
    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            **data,
            "active_tab": "analytics"
        }
    )


# =========================================================================
# 5. COMMERCIAL AWARD DECISION & ALLOCATION
# =========================================================================
@router.get("/rfqs/{rfq_id}/award", response_class=HTMLResponse)
@router.get("/comparisons/{comparison_id}/award", response_class=HTMLResponse)
async def award_decision_page(
    request: Request,
    rfq_id: Optional[str] = None,
    comparison_id: Optional[str] = None,
    scenario: Optional[str] = None
):
    if not rfq_id and comparison_id:
        comp_record = services.get_comparison(comparison_id)
        if comp_record:
            rfq_id = comp_record.get("rfq_id")
        else:
            raise HTTPException(status_code=404, detail="Comparison not found")

    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    comparison = services.get_latest_rfq_comparison(rfq_id)
    if not comparison:
        quotes = services.get_quotes_for_rfq(rfq_id)
        if quotes:
            return RedirectResponse(f"/comparisons/create?rfq_id={rfq_id}", status_code=303)
        return RedirectResponse(f"/rfqs/{rfq_id}", status_code=303)

    award = services.build_proposed_award_allocation(rfq_id, comparison["comparison_id"], scenario or "SINGLE_SUPPLIER_L1")

    # Detect if a newer comparison snapshot exists than what a finalized award was evaluated on
    has_newer_comparison = False
    newer_comparison_id = None
    if award.get("status") == "FINALIZED" and comparison:
        award_comp_id = award.get("comparison_id")
        if award_comp_id and award_comp_id != comparison.get("comparison_id"):
            has_newer_comparison = True
            newer_comparison_id = comparison.get("comparison_id")

    quotes = services.get_quotes_for_rfq(rfq_id)
    total_quotes_count = len(quotes)

    return templates.TemplateResponse(
        request=request,
        name="award/decision.html",
        context={
            "rfq": rfq,
            "comparison": comparison,
            "award": award,
            "total_quotes_count": total_quotes_count,
            "has_newer_comparison": has_newer_comparison,
            "newer_comparison_id": newer_comparison_id,
            "active_tab": "comparisons"
        }
    )


@router.post("/rfqs/{rfq_id}/award/finalize")
async def finalize_award_action(
    rfq_id: str,
    selected_scenario: str = Form("MANUAL_ALLOCATION"),
    base_currency: str = Form("INR"),
    allocations_json: Optional[str] = Form(None),
    allocation_json: Optional[str] = Form(None),
    buyer_accepted_unallocated: Optional[str] = Form(None),
    award_notes: Optional[str] = Form(None)
):
    raw_json = allocations_json or allocation_json or "[]"
    try:
        allocations = json.loads(raw_json)
    except Exception:
        allocations = []

    accepted_bool = str(buyer_accepted_unallocated or "").lower() in ["true", "on", "1", "yes"]

    award_data = {
        "rfq_id": rfq_id,
        "selected_scenario": selected_scenario,
        "base_currency": base_currency,
        "buyer_accepted_unallocated": accepted_bool,
        "allocations": allocations,
        "award_notes": award_notes
    }

    services.save_award_decision(rfq_id, award_data, is_finalized=True)
    return RedirectResponse(f"/rfqs/{rfq_id}/award", status_code=303)


@router.post("/rfqs/{rfq_id}/award/reopen")
async def reopen_award_action(
    rfq_id: str,
    reason: Optional[str] = Form("Buyer requested allocation revision")
):
    services.reopen_award_decision(rfq_id, reason=reason or "Buyer requested allocation revision")
    return RedirectResponse(f"/rfqs/{rfq_id}/award", status_code=303)


# =========================================================================
# 7. PROCUREMENT EXECUTION & AWARD EXPORT ARTIFACTS
# =========================================================================

@router.get("/rfqs/{rfq_id}/award/export/excel")
async def export_award_excel_endpoint(rfq_id: str):
    from award import exporter
    try:
        content = exporter.generate_award_excel_workbook(rfq_id)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{rfq_id}_Award_Workbook.xlsx"'}
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/rfqs/{rfq_id}/award/export/pdf")
async def export_award_pdf_endpoint(rfq_id: str):
    from award import exporter
    try:
        content = exporter.generate_award_executive_pdf(rfq_id)
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{rfq_id}_Executive_Award_Report.pdf"'}
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/rfqs/{rfq_id}/award/export/csv")
async def export_award_csv_endpoint(rfq_id: str):
    from award import exporter
    try:
        content = exporter.generate_award_csv_export(rfq_id)
        return Response(
            content=content.encode("utf-8"),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{rfq_id}_Award_Allocation.csv"'}
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/rfqs/{rfq_id}/award/export/requisition/{supplier_id}/excel")
async def export_supplier_requisition_excel_endpoint(rfq_id: str, supplier_id: str):
    from award import exporter
    try:
        content = exporter.generate_supplier_award_requisition_excel(rfq_id, supplier_id)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="REQ_{rfq_id}_{supplier_id}.xlsx"'}
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/rfqs/{rfq_id}/award/export/requisition/{supplier_id}/pdf")
async def export_supplier_requisition_pdf_endpoint(rfq_id: str, supplier_id: str):
    from award import exporter
    try:
        content = exporter.generate_supplier_award_requisition_pdf(rfq_id, supplier_id)
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="REQ_{rfq_id}_{supplier_id}.pdf"'}
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

