"""
Procurement Execution Artifacts & Exporter Engine.
Generates:
1. Executive Excel Award Workbook (4 sheets with executive summary, line allocations, supplier summaries, audit logs).
2. Supplier-Specific Purchase Order Requisitions (Excel & PDF).
3. Executive PDF Award Report for Management.
4. CSV Allocation Export for ERP / P2P ingest.
"""

from datetime import datetime, timezone
from decimal import Decimal
import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app import services
from core.canonical_quote import quantize_currency


# =========================================================================
# 1. EXCEL AWARD WORKBOOK (4 SHEETS)
# =========================================================================

def generate_award_excel_workbook(rfq_id: str) -> bytes:
    """
    Generates a multi-tab Excel Award Workbook:
    - Sheet 1: Executive Summary
    - Sheet 2: Award Allocation
    - Sheet 3: Supplier Summary
    - Sheet 4: Audit & Provenance
    """
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    award = services.get_award_decision(rfq_id)
    if not award:
        comp = services.get_latest_rfq_comparison(rfq_id)
        if not comp:
            raise ValueError(f"No comparison or award decision available for RFQ {rfq_id}.")
        award = services._generate_fresh_award_proposal(rfq_id, comp)

    wb = openpyxl.Workbook()
    
    # Styles
    navy_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    accent_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
    subtle_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    green_fill = PatternFill(start_color="ECFDF5", end_color="ECFDF5", fill_type="solid")
    
    white_bold = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    navy_bold = Font(name="Calibri", size=11, bold=True, color="0F172A")
    title_font = Font(name="Calibri", size=15, bold=True, color="0F172A")
    subtitle_font = Font(name="Calibri", size=10, italic=True, color="64748B")
    data_font = Font(name="Calibri", size=10, color="1E293B")
    bold_data_font = Font(name="Calibri", size=10, bold=True, color="0F172A")
    mono_data_font = Font(name="Consolas", size=10, color="0F172A")
    
    thin_border_side = Side(style="thin", color="CBD5E1")
    grid_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)

    # -------------------------------------------------------------------------
    # Sheet 1: Executive Summary
    # -------------------------------------------------------------------------
    ws1 = wb.active
    ws1.title = "Executive Summary"
    ws1.views.sheetView[0].showGridLines = True
    
    ws1.append(["PROCUREMENT AWARD DECISION — EXECUTIVE SUMMARY"])
    ws1["A1"].font = title_font
    ws1.append([f"Authoritative evaluation snapshot generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
    ws1["A2"].font = subtitle_font
    ws1.append([])

    summary_metadata = [
        ("RFQ Reference Number", rfq.rfq_id),
        ("RFQ Project Title", rfq.title or "Procurement Project"),
        ("Award Status", award.get("status", "DRAFT")),
        ("Award Baseline Date", award.get("awarded_at") or "Pending Finalization"),
        ("Awarded By", award.get("awarded_by", "Procurement Specialist")),
        ("Base Sourcing Currency", award.get("base_currency", "INR")),
        ("Total Award Commitment", f"{award.get('base_currency', 'INR')} {Decimal(str(award.get('total_awarded_value', 0))):,.2f}"),
        ("Total RFQ Scope Items", award.get("total_required_items", len(rfq.items))),
        ("Fully Allocated Lines", award.get("fully_allocated_items_count", 0)),
        ("Partially Allocated Lines", award.get("partially_allocated_items_count", 0)),
        ("Unallocated Scope Lines", award.get("unallocated_items_count", 0)),
        ("Selected Sourcing Strategy", award.get("selected_scenario", "MANUAL_ALLOCATION")),
        ("Commercial Evaluation ID", award.get("comparison_id", "N/A")),
    ]

    ws1.append(["Parameter", "Value"])
    ws1["A4"].fill = navy_fill
    ws1["A4"].font = white_bold
    ws1["B4"].fill = navy_fill
    ws1["B4"].font = white_bold

    for row_idx, (k, v) in enumerate(summary_metadata, start=5):
        ws1.append([k, v])
        ws1[f"A{row_idx}"].font = bold_data_font
        ws1[f"B{row_idx}"].font = data_font
        ws1[f"A{row_idx}"].border = grid_border
        ws1[f"B{row_idx}"].border = grid_border
        if row_idx % 2 == 0:
            ws1[f"A{row_idx}"].fill = subtle_fill
            ws1[f"B{row_idx}"].fill = subtle_fill

    ws1.column_dimensions["A"].width = 30
    ws1.column_dimensions["B"].width = 50

    # -------------------------------------------------------------------------
    # Sheet 2: Award Allocation
    # -------------------------------------------------------------------------
    ws2 = wb.create_sheet(title="Award Allocation")
    ws2.views.sheetView[0].showGridLines = True
    
    alloc_headers = [
        "RFQ Line ID", "Item SKU", "Description & Specifications", "Required Qty", "UOM",
        "Awarded Supplier", "Quote Reference", "Awarded Qty", "Unit Landed Cost",
        "Line Award Value", "Currency", "Allocation State", "L1 Line Optimal"
    ]
    ws2.append(alloc_headers)
    for col_idx in range(1, len(alloc_headers) + 1):
        cell = ws2.cell(row=1, column=col_idx)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center", vertical="center")

    curr_row = 2
    base_cur = award.get("base_currency", "INR")
    allocations = award.get("allocations", [])

    for alloc in allocations:
        line_id = alloc.get("rfq_line_id", "")
        sku = alloc.get("item_sku", "")
        desc = alloc.get("item_description", "")
        req_qty = float(alloc.get("required_qty", 0))
        uom = alloc.get("required_uom", "PCS")
        alloc_state = alloc.get("allocation_state", "UNALLOCATED")
        splits = alloc.get("supplier_splits", [])

        if splits:
            for sp in splits:
                sname = sp.get("supplier_name") or sp.get("supplier_id")
                qid = sp.get("quote_id", "")
                aqty = float(sp.get("allocated_qty", 0))
                landed = float(sp.get("unit_landed_cost", 0))
                sval = float(sp.get("split_value", aqty * landed))
                is_l1 = "YES" if sp.get("is_l1_for_line") else "NO"

                ws2.append([
                    line_id, sku, desc, req_qty, uom,
                    sname, qid, aqty, landed, sval, base_cur, alloc_state, is_l1
                ])
                
                ws2.cell(row=curr_row, column=4).number_format = "#,##0.00"
                ws2.cell(row=curr_row, column=8).number_format = "#,##0.00"
                ws2.cell(row=curr_row, column=9).number_format = "#,##0.0000"
                ws2.cell(row=curr_row, column=10).number_format = "#,##0.00"
                for c in range(1, len(alloc_headers) + 1):
                    ws2.cell(row=curr_row, column=c).border = grid_border
                    ws2.cell(row=curr_row, column=c).font = data_font
                curr_row += 1
        else:
            ws2.append([
                line_id, sku, desc, req_qty, uom,
                "UNALLOCATED", "N/A", 0.0, 0.0, 0.0, base_cur, alloc_state, "NO"
            ])
            for c in range(1, len(alloc_headers) + 1):
                ws2.cell(row=curr_row, column=c).border = grid_border
                ws2.cell(row=curr_row, column=c).font = data_font
            curr_row += 1

    for col in ws2.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws2.column_dimensions[col_letter].width = max(max_len + 3, 12)

    # -------------------------------------------------------------------------
    # Sheet 3: Supplier Summary
    # -------------------------------------------------------------------------
    ws3 = wb.create_sheet(title="Supplier Summary")
    ws3.views.sheetView[0].showGridLines = True
    
    supp_headers = ["Supplier Name", "Quote ID", "Awarded Line Items", "Total Awarded Quantity", "Total Awarded Value", "Percentage Share (%)"]
    ws3.append(supp_headers)
    for col_idx in range(1, len(supp_headers) + 1):
        cell = ws3.cell(row=1, column=col_idx)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center", vertical="center")

    total_award_val = Decimal(str(award.get("total_awarded_value", 0)))
    supplier_agg: Dict[str, Dict[str, Any]] = {}

    for alloc in allocations:
        for sp in alloc.get("supplier_splits", []):
            sid = sp.get("supplier_id")
            sname = sp.get("supplier_name") or sid
            qid = sp.get("quote_id", sid)
            sp_qty = Decimal(str(sp.get("allocated_qty", 0)))
            sp_val = Decimal(str(sp.get("split_value", 0)))

            if sname not in supplier_agg:
                supplier_agg[sname] = {
                    "quote_id": qid,
                    "lines_count": 0,
                    "total_qty": Decimal("0"),
                    "total_val": Decimal("0")
                }
            supplier_agg[sname]["lines_count"] += 1
            supplier_agg[sname]["total_qty"] += sp_qty
            supplier_agg[sname]["total_val"] += sp_val

    s_row = 2
    for sname, sdata in sorted(supplier_agg.items(), key=lambda x: x[1]["total_val"], reverse=True):
        share_pct = float((sdata["total_val"] / total_award_val * Decimal("100.0")) if total_award_val > 0 else Decimal("0"))
        ws3.append([
            sname, sdata["quote_id"], sdata["lines_count"],
            float(sdata["total_qty"]), float(sdata["total_val"]), share_pct
        ])
        ws3.cell(row=s_row, column=4).number_format = "#,##0.00"
        ws3.cell(row=s_row, column=5).number_format = "#,##0.00"
        ws3.cell(row=s_row, column=6).number_format = "0.00%"
        for c in range(1, len(supp_headers) + 1):
            ws3.cell(row=s_row, column=c).border = grid_border
            ws3.cell(row=s_row, column=c).font = data_font
        s_row += 1

    for col in ws3.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws3.column_dimensions[col_letter].width = max(max_len + 3, 14)

    # -------------------------------------------------------------------------
    # Sheet 4: Audit & Provenance
    # -------------------------------------------------------------------------
    ws4 = wb.create_sheet(title="Audit & Provenance")
    ws4.views.sheetView[0].showGridLines = True
    
    ws4.append(["AUDIT & REVISION PROVENANCE LOG"])
    ws4["A1"].font = title_font
    ws4.append([])

    audit_meta = [
        ("Canonical RFQ ID", rfq.rfq_id),
        ("Award ID", award.get("award_id", f"AWD-{rfq_id}")),
        ("Commercial Comparison ID", award.get("comparison_id", "N/A")),
        ("Current Award Status", award.get("status", "DRAFT")),
        ("Finalization Timestamp (UTC)", award.get("awarded_at") or "Not Finalized"),
        ("Award Decision Maker", award.get("awarded_by", "Procurement Specialist")),
        ("Total Revisions / Reopens", len(award.get("reopen_history", []))),
    ]
    
    ws4.append(["Audit Metadata Key", "System Record Value"])
    ws4["A3"].fill = navy_fill
    ws4["A3"].font = white_bold
    ws4["B3"].fill = navy_fill
    ws4["B3"].font = white_bold

    for row_idx, (k, v) in enumerate(audit_meta, start=4):
        ws4.append([k, str(v)])
        ws4[f"A{row_idx}"].font = bold_data_font
        ws4[f"B{row_idx}"].font = data_font
        ws4[f"A{row_idx}"].border = grid_border
        ws4[f"B{row_idx}"].border = grid_border

    reopen_hist = award.get("reopen_history", [])
    if reopen_hist:
        ws4.append([])
        ws4.append(["REVISION & REOPEN EVENT HISTORY"])
        hist_title_row = ws4.max_row
        ws4[f"A{hist_title_row}"].font = navy_bold
        
        hist_headers = ["Revision #", "Reopened At", "Reopened By", "Previous Status", "Previous Comparison", "Previous Value", "Buyer Revision Reason"]
        ws4.append(hist_headers)
        h_row = ws4.max_row
        for c in range(1, len(hist_headers) + 1):
            ws4.cell(row=h_row, column=c).fill = accent_fill
            ws4.cell(row=h_row, column=c).font = white_bold

        for idx, h in enumerate(reopen_hist, start=1):
            ws4.append([
                idx, h.get("reopened_at", ""), h.get("reopened_by", ""),
                h.get("previous_status", ""), h.get("previous_comparison_id", ""),
                float(h.get("previous_total_awarded_value", 0)), h.get("reopen_reason", "")
            ])
            r_idx = ws4.max_row
            ws4.cell(row=r_idx, column=6).number_format = "#,##0.00"
            for c in range(1, len(hist_headers) + 1):
                ws4.cell(row=r_idx, column=c).border = grid_border
                ws4.cell(row=r_idx, column=c).font = data_font

    ws4.column_dimensions["A"].width = 28
    ws4.column_dimensions["B"].width = 40
    ws4.column_dimensions["C"].width = 24
    ws4.column_dimensions["D"].width = 20
    ws4.column_dimensions["E"].width = 28
    ws4.column_dimensions["F"].width = 20
    ws4.column_dimensions["G"].width = 45

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =========================================================================
# 2. SUPPLIER-SPECIFIC PURCHASE ORDER REQUISITION (EXCEL & PDF)
# =========================================================================

def generate_supplier_award_requisition_excel(rfq_id: str, supplier_identifier: str) -> bytes:
    """
    Generates a dedicated Purchase Order Requisition Excel workbook for an individual awarded supplier.
    """
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    award = services.get_award_decision(rfq_id)
    if not award:
        comp = services.get_latest_rfq_comparison(rfq_id)
        if not comp:
            raise ValueError(f"No award decision available for RFQ {rfq_id}.")
        award = services._generate_fresh_award_proposal(rfq_id, comp)

    supplier_lines = []
    supplier_name = supplier_identifier
    supplier_quote_id = supplier_identifier
    total_supp_val = Decimal("0")

    for alloc in award.get("allocations", []):
        for sp in alloc.get("supplier_splits", []):
            if sp.get("supplier_id") == supplier_identifier or sp.get("quote_id") == supplier_identifier or sp.get("supplier_name") == supplier_identifier:
                supplier_name = sp.get("supplier_name", supplier_identifier)
                supplier_quote_id = sp.get("quote_id", supplier_identifier)
                sp_qty = Decimal(str(sp.get("allocated_qty", 0)))
                sp_landed = Decimal(str(sp.get("unit_landed_cost", 0)))
                sp_val = Decimal(str(sp.get("split_value", sp_qty * sp_landed)))
                total_supp_val += sp_val
                supplier_lines.append({
                    "rfq_line_id": alloc.get("rfq_line_id"),
                    "item_sku": alloc.get("item_sku"),
                    "item_description": alloc.get("item_description"),
                    "allocated_qty": sp_qty,
                    "uom": alloc.get("required_uom", "PCS"),
                    "unit_landed_cost": sp_landed,
                    "line_value": sp_val,
                    "quoted_capacity": sp.get("quoted_capacity")
                })

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Award Requisition"
    ws.views.sheetView[0].showGridLines = True

    navy_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    white_bold = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=14, bold=True, color="0F172A")
    disclaimer_font = Font(name="Calibri", size=9, italic=True, color="B45309")
    bold_font = Font(name="Calibri", size=10, bold=True, color="0F172A")
    data_font = Font(name="Calibri", size=10, color="1E293B")
    
    thin_border = Border(left=Side(style="thin", color="CBD5E1"), right=Side(style="thin", color="CBD5E1"),
                         top=Side(style="thin", color="CBD5E1"), bottom=Side(style="thin", color="CBD5E1"))

    ws.append(["PURCHASE ORDER REQUISITION — EVALUATED AWARD NOTICE"])
    ws["A1"].font = title_font
    ws.append(["NOTICE: THIS IS AN INTERNAL PROCUREMENT AWARD REQUISITION, NOT A LEGALLY BINDING PURCHASE ORDER."])
    ws["A2"].font = disclaimer_font
    ws.append([])

    req_id = f"REQ-{rfq_id}-{supplier_quote_id}"
    meta = [
        ("Requisition Reference", req_id),
        ("RFQ Reference Number", rfq.rfq_id),
        ("RFQ Project Title", rfq.title or "Industrial Procurement"),
        ("Awarded Vendor / Supplier", supplier_name),
        ("Supplier Quote Reference", supplier_quote_id),
        ("Requisition Issue Date", datetime.now().strftime("%Y-%m-%d")),
        ("Award Baseline Comparison", award.get("comparison_id", "N/A")),
        ("Base Sourcing Currency", award.get("base_currency", "INR")),
        ("Total Requisition Commitment", f"{award.get('base_currency', 'INR')} {total_supp_val:,.2f}")
    ]

    for k, v in meta:
        ws.append([k, str(v)])
        r_idx = ws.max_row
        ws[f"A{r_idx}"].font = bold_font
        ws[f"B{r_idx}"].font = data_font
        ws[f"A{r_idx}"].border = thin_border
        ws[f"B{r_idx}"].border = thin_border

    ws.append([])
    ws.append(["AWARDED LINE ITEM SCHEDULE"])
    sched_title_row = ws.max_row
    ws[f"A{sched_title_row}"].font = bold_font

    table_headers = ["Line #", "RFQ Line ID", "Item SKU", "Description & Specifications", "Awarded Qty", "UOM", "Unit Evaluated Price", "Extended Total", "Currency"]
    ws.append(table_headers)
    hdr_row = ws.max_row
    for c in range(1, len(table_headers) + 1):
        ws.cell(row=hdr_row, column=c).fill = navy_fill
        ws.cell(row=hdr_row, column=c).font = white_bold
        ws.cell(row=hdr_row, column=c).alignment = Alignment(horizontal="center", vertical="center")

    for idx, item in enumerate(supplier_lines, start=1):
        ws.append([
            idx, item["rfq_line_id"], item["item_sku"], item["item_description"],
            float(item["allocated_qty"]), item["uom"], float(item["unit_landed_cost"]),
            float(item["line_value"]), award.get("base_currency", "INR")
        ])
        curr = ws.max_row
        ws.cell(row=curr, column=5).number_format = "#,##0.00"
        ws.cell(row=curr, column=7).number_format = "#,##0.0000"
        ws.cell(row=curr, column=8).number_format = "#,##0.00"
        for c in range(1, len(table_headers) + 1):
            ws.cell(row=curr, column=c).border = thin_border
            ws.cell(row=curr, column=c).font = data_font

    # Total row
    ws.append(["", "", "", "TOTAL REQUISITION VALUE", "", "", "", float(total_supp_val), award.get("base_currency", "INR")])
    tot_row = ws.max_row
    ws.cell(row=tot_row, column=4).font = bold_font
    ws.cell(row=tot_row, column=8).font = bold_font
    ws.cell(row=tot_row, column=8).number_format = "#,##0.00"
    for c in range(1, len(table_headers) + 1):
        ws.cell(row=tot_row, column=c).border = thin_border

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_supplier_award_requisition_pdf(rfq_id: str, supplier_identifier: str) -> bytes:
    """
    Generates a PDF Purchase Order Requisition document for an individual supplier.
    """
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    award = services.get_award_decision(rfq_id)
    if not award:
        comp = services.get_latest_rfq_comparison(rfq_id)
        if not comp:
            raise ValueError(f"No award decision available for RFQ {rfq_id}.")
        award = services._generate_fresh_award_proposal(rfq_id, comp)

    supplier_lines = []
    supplier_name = supplier_identifier
    supplier_quote_id = supplier_identifier
    total_supp_val = Decimal("0")

    for alloc in award.get("allocations", []):
        for sp in alloc.get("supplier_splits", []):
            if sp.get("supplier_id") == supplier_identifier or sp.get("quote_id") == supplier_identifier or sp.get("supplier_name") == supplier_identifier:
                supplier_name = sp.get("supplier_name", supplier_identifier)
                supplier_quote_id = sp.get("quote_id", supplier_identifier)
                sp_qty = Decimal(str(sp.get("allocated_qty", 0)))
                sp_landed = Decimal(str(sp.get("unit_landed_cost", 0)))
                sp_val = Decimal(str(sp.get("split_value", sp_qty * sp_landed)))
                total_supp_val += sp_val
                supplier_lines.append({
                    "rfq_line_id": alloc.get("rfq_line_id"),
                    "item_sku": alloc.get("item_sku"),
                    "item_description": alloc.get("item_description"),
                    "allocated_qty": sp_qty,
                    "uom": alloc.get("required_uom", "PCS"),
                    "unit_landed_cost": sp_landed,
                    "line_value": sp_val
                })

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle("ReqTitle", parent=styles["Heading1"], fontSize=16, leading=20, textColor=colors.HexColor("#0F172A"), spaceAfter=2)
    notice_style = ParagraphStyle("ReqNotice", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#B45309"), spaceAfter=10)
    body_style = ParagraphStyle("ReqBody", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#1E293B"))
    bold_style = ParagraphStyle("ReqBold", parent=body_style, fontName="Helvetica-Bold", textColor=colors.HexColor("#0F172A"))
    header_table_style = ParagraphStyle("ReqHdrTbl", parent=body_style, fontName="Helvetica-Bold", textColor=colors.white, alignment=1)

    story = []
    story.append(Paragraph("PURCHASE ORDER REQUISITION", title_style))
    story.append(Paragraph("<b>Notice:</b> This document represents an internal evaluated award allocation notice, not a formally issued purchase order.", notice_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2563EB"), spaceAfter=10))

    base_cur = award.get("base_currency", "INR")
    req_id = f"REQ-{rfq_id}-{supplier_quote_id}"

    meta_data = [
        [Paragraph("<b>Requisition Ref:</b>", body_style), Paragraph(req_id, bold_style), Paragraph("<b>Issue Date:</b>", body_style), Paragraph(datetime.now().strftime("%Y-%m-%d"), body_style)],
        [Paragraph("<b>RFQ Project:</b>", body_style), Paragraph(f"{rfq.rfq_id} - {rfq.title or ''}", body_style), Paragraph("<b>Total Commitment:</b>", body_style), Paragraph(f"<b>{base_cur} {total_supp_val:,.2f}</b>", bold_style)],
        [Paragraph("<b>Awarded Supplier:</b>", body_style), Paragraph(supplier_name, bold_style), Paragraph("<b>Quote Ref:</b>", body_style), Paragraph(supplier_quote_id, body_style)],
    ]
    t_meta = Table(meta_data, colWidths=[110, 155, 110, 145])
    t_meta.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t_meta)
    story.append(Spacer(1, 14))

    table_data = [[
        Paragraph("Line #", header_table_style),
        Paragraph("SKU", header_table_style),
        Paragraph("Description", header_table_style),
        Paragraph("Awarded Qty", header_table_style),
        Paragraph(f"Unit Landed ({base_cur})", header_table_style),
        Paragraph(f"Extended Value ({base_cur})", header_table_style)
    ]]

    for idx, item in enumerate(supplier_lines, start=1):
        table_data.append([
            Paragraph(str(idx), body_style),
            Paragraph(item["item_sku"], bold_style),
            Paragraph(item["item_description"], body_style),
            Paragraph(f"{item['allocated_qty']:,.0f} {item['uom']}", bold_style),
            Paragraph(f"{item['unit_landed_cost']:,.2f}", body_style),
            Paragraph(f"{item['line_value']:,.2f}", bold_style)
        ])

    table_data.append([
        Paragraph("", body_style),
        Paragraph("<b>TOTAL</b>", bold_style),
        Paragraph("", body_style),
        Paragraph("", body_style),
        Paragraph("", body_style),
        Paragraph(f"<b>{base_cur} {total_supp_val:,.2f}</b>", bold_style)
    ])

    t_lines = Table(table_data, colWidths=[40, 85, 185, 70, 70, 70])
    t_lines.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    story.append(t_lines)

    doc.build(story)
    return buf.getvalue()


# =========================================================================
# 3. EXECUTIVE PDF AWARD REPORT (REPORTLAB)
# =========================================================================

def generate_award_executive_pdf(rfq_id: str) -> bytes:
    """
    Generates a PDF Executive Award Report suitable for procurement leadership.
    """
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    award = services.get_award_decision(rfq_id)
    if not award:
        comp = services.get_latest_rfq_comparison(rfq_id)
        if not comp:
            raise ValueError(f"No award decision available for RFQ {rfq_id}.")
        award = services._generate_fresh_award_proposal(rfq_id, comp)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0F172A"),
        spaceAfter=4
    )
    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#64748B"),
        spaceAfter=14
    )
    h2_style = ParagraphStyle(
        "SectionH2",
        parent=styles["Heading2"],
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#1E293B"),
        spaceBefore=10,
        spaceAfter=6
    )
    body_style = ParagraphStyle(
        "BodyDark",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#1E293B")
    )
    bold_style = ParagraphStyle(
        "BoldDark",
        parent=body_style,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#0F172A")
    )
    header_table_style = ParagraphStyle(
        "HeaderTable",
        parent=body_style,
        fontName="Helvetica-Bold",
        textColor=colors.white,
        alignment=1
    )

    story = []

    # Title & Metadata
    story.append(Paragraph("EXECUTIVE PROCUREMENT AWARD REPORT", title_style))
    story.append(Paragraph(f"Authoritative Award Evaluation Baseline &bull; Project: {rfq.title or rfq.rfq_id}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2563EB"), spaceAfter=10))

    total_val = Decimal(str(award.get("total_awarded_value", 0)))
    base_cur = award.get("base_currency", "INR")
    
    meta_data = [
        [
            Paragraph("<b>RFQ ID:</b>", body_style), Paragraph(rfq.rfq_id, body_style),
            Paragraph("<b>Award Status:</b>", body_style), Paragraph(f"<b>{award.get('status', 'DRAFT')}</b>", bold_style)
        ],
        [
            Paragraph("<b>Total Award Commitment:</b>", body_style), Paragraph(f"<b>{base_cur} {total_val:,.2f}</b>", bold_style),
            Paragraph("<b>Award Date:</b>", body_style), Paragraph(award.get("awarded_at") or "Pending Finalization", body_style)
        ],
        [
            Paragraph("<b>Strategy / Scenario:</b>", body_style), Paragraph(award.get("selected_scenario", "MANUAL_ALLOCATION"), body_style),
            Paragraph("<b>Comparison ID:</b>", body_style), Paragraph(award.get("comparison_id", "N/A"), body_style)
        ],
        [
            Paragraph("<b>Scope Fulfillment:</b>", body_style), Paragraph(f"{award.get('fully_allocated_items_count', 0)} / {award.get('total_required_items', len(rfq.items))} Lines Fully Allocated", body_style),
            Paragraph("<b>Evaluated By:</b>", body_style), Paragraph(award.get("awarded_by", "Procurement Specialist"), body_style)
        ]
    ]

    meta_table = Table(meta_data, colWidths=[120, 145, 110, 145])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 12))

    # Supplier Allocation Summary
    story.append(Paragraph("1. Supplier Allocation Summary", h2_style))
    
    allocations = award.get("allocations", [])
    supplier_agg: Dict[str, Dict[str, Any]] = {}
    for alloc in allocations:
        for sp in alloc.get("supplier_splits", []):
            sname = sp.get("supplier_name") or sp.get("supplier_id")
            qid = sp.get("quote_id", "")
            sp_qty = Decimal(str(sp.get("allocated_qty", 0)))
            sp_val = Decimal(str(sp.get("split_value", 0)))

            if sname not in supplier_agg:
                supplier_agg[sname] = {"quote_id": qid, "lines": 0, "qty": Decimal("0"), "val": Decimal("0")}
            supplier_agg[sname]["lines"] += 1
            supplier_agg[sname]["qty"] += sp_qty
            supplier_agg[sname]["val"] += sp_val

    supp_table_data = [[
        Paragraph("Awarded Supplier", header_table_style),
        Paragraph("Quote Ref", header_table_style),
        Paragraph("Awarded Lines", header_table_style),
        Paragraph("Total Quantity", header_table_style),
        Paragraph(f"Award Value ({base_cur})", header_table_style),
        Paragraph("Share (%)", header_table_style)
    ]]

    for sname, sdata in sorted(supplier_agg.items(), key=lambda x: x[1]["val"], reverse=True):
        share_pct = (sdata["val"] / total_val * Decimal("100.0")) if total_val > 0 else Decimal("0")
        supp_table_data.append([
            Paragraph(sname, bold_style),
            Paragraph(sdata["quote_id"], body_style),
            Paragraph(str(sdata["lines"]), body_style),
            Paragraph(f"{sdata['qty']:,.2f}", body_style),
            Paragraph(f"{sdata['val']:,.2f}", bold_style),
            Paragraph(f"{share_pct:.1f}%", body_style)
        ])

    supp_table = Table(supp_table_data, colWidths=[140, 90, 65, 75, 95, 55])
    supp_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(supp_table)
    story.append(Spacer(1, 12))

    # Line Item Allocation Details
    story.append(Paragraph("2. Line-Level Award Allocation Details", h2_style))
    
    line_table_data = [[
        Paragraph("Line ID", header_table_style),
        Paragraph("SKU", header_table_style),
        Paragraph("Required Qty", header_table_style),
        Paragraph("Awarded Supplier", header_table_style),
        Paragraph("Awarded Qty", header_table_style),
        Paragraph(f"Landed Cost ({base_cur})", header_table_style),
        Paragraph(f"Extended ({base_cur})", header_table_style)
    ]]

    for alloc in allocations:
        line_id = alloc.get("rfq_line_id", "")
        sku = alloc.get("item_sku", "")
        req_qty = float(alloc.get("required_qty", 0))
        uom = alloc.get("required_uom", "PCS")
        splits = alloc.get("supplier_splits", [])

        if splits:
            for sp in splits:
                sname = sp.get("supplier_name") or sp.get("supplier_id")
                aqty = float(sp.get("allocated_qty", 0))
                landed = float(sp.get("unit_landed_cost", 0))
                sval = float(sp.get("split_value", aqty * landed))

                line_table_data.append([
                    Paragraph(line_id, body_style),
                    Paragraph(sku, bold_style),
                    Paragraph(f"{req_qty:,.0f} {uom}", body_style),
                    Paragraph(sname, body_style),
                    Paragraph(f"{aqty:,.0f} {uom}", bold_style),
                    Paragraph(f"{landed:,.2f}", body_style),
                    Paragraph(f"{sval:,.2f}", bold_style)
                ])
        else:
            line_table_data.append([
                Paragraph(line_id, body_style),
                Paragraph(sku, bold_style),
                Paragraph(f"{req_qty:,.0f} {uom}", body_style),
                Paragraph("<font color='red'>UNALLOCATED</font>", body_style),
                Paragraph("0", body_style),
                Paragraph("0.00", body_style),
                Paragraph("0.00", body_style)
            ])

    line_table = Table(line_table_data, colWidths=[65, 85, 65, 125, 65, 55, 60])
    line_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(line_table)
    story.append(Spacer(1, 12))

    # Audit & Reopen History
    reopen_hist = award.get("reopen_history", [])
    if reopen_hist:
        story.append(Paragraph("3. Decision Revision & Reopen History", h2_style))
        hist_data = [[
            Paragraph("Rev #", header_table_style),
            Paragraph("Reopened At", header_table_style),
            Paragraph("Reopened By", header_table_style),
            Paragraph("Previous Status", header_table_style),
            Paragraph("Previous Value", header_table_style),
            Paragraph("Revision Reason", header_table_style)
        ]]
        for idx, h in enumerate(reopen_hist, start=1):
            hist_data.append([
                Paragraph(str(idx), body_style),
                Paragraph(h.get("reopened_at", "")[:19].replace("T", " "), body_style),
                Paragraph(h.get("reopened_by", ""), body_style),
                Paragraph(h.get("previous_status", ""), body_style),
                Paragraph(f"{base_cur} {float(h.get('previous_total_awarded_value', 0)):,.2f}", bold_style),
                Paragraph(h.get("reopen_reason", ""), body_style)
            ])
        hist_table = Table(hist_data, colWidths=[35, 95, 85, 75, 80, 150])
        hist_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(hist_table)

    doc.build(story)
    return buf.getvalue()


# =========================================================================
# 4. CSV PROCUREMENT ALLOCATION EXPORT
# =========================================================================

def generate_award_csv_export(rfq_id: str) -> str:
    """
    Generates a deterministic flat CSV string for ERP / P2P ingest.
    """
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    award = services.get_award_decision(rfq_id)
    if not award:
        comp = services.get_latest_rfq_comparison(rfq_id)
        if not comp:
            raise ValueError(f"No award decision available for RFQ {rfq_id}.")
        award = services._generate_fresh_award_proposal(rfq_id, comp)

    lines = [
        "rfq_id,rfq_line_id,item_sku,item_description,required_qty,required_uom,supplier_name,supplier_id,quote_id,awarded_qty,unit_landed_cost,line_total_value,base_currency,comparison_id,award_id,awarded_at"
    ]

    base_cur = award.get("base_currency", "INR")
    comp_id = award.get("comparison_id", "")
    award_id = award.get("award_id", f"AWD-{rfq_id}")
    awarded_at = award.get("awarded_at", "")

    for alloc in award.get("allocations", []):
        line_id = alloc.get("rfq_line_id", "")
        sku = alloc.get("item_sku", "").replace(",", ";")
        desc = alloc.get("item_description", "").replace(",", ";")
        req_qty = alloc.get("required_qty", "0")
        uom = alloc.get("required_uom", "PCS")
        splits = alloc.get("supplier_splits", [])

        if splits:
            for sp in splits:
                sname = (sp.get("supplier_name") or sp.get("supplier_id", "")).replace(",", ";")
                sid = sp.get("supplier_id", "")
                qid = sp.get("quote_id", "")
                aqty = sp.get("allocated_qty", "0")
                landed = sp.get("unit_landed_cost", "0")
                sval = sp.get("split_value", "0")

                lines.append(f"{rfq_id},{line_id},{sku},{desc},{req_qty},{uom},{sname},{sid},{qid},{aqty},{landed},{sval},{base_cur},{comp_id},{award_id},{awarded_at}")
        else:
            lines.append(f"{rfq_id},{line_id},{sku},{desc},{req_qty},{uom},UNALLOCATED,N/A,N/A,0,0.00,0.00,{base_cur},{comp_id},{award_id},{awarded_at}")

    return "\n".join(lines)
