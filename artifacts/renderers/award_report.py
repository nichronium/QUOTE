"""
Domain-Agnostic Executive Award Report PDF Renderer.
Consumes pre-computed AwardRecordViewModel. Performs NO business calculations.
Implements executive visual hierarchy, dimension-safe supplier summaries, and provenance trails.
"""

from datetime import datetime, timezone
import io
from typing import Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from artifacts.models import ArtifactFormat, ArtifactMetadata, ArtifactType
from artifacts.registry import BaseArtifactRenderer, register_renderer
from artifacts.view_models import AwardRecordViewModel


class AwardReportRenderer(BaseArtifactRenderer):
    artifact_type = ArtifactType.AWARD_REPORT

    def render(self, vm: AwardRecordViewModel, **kwargs) -> Tuple[bytes, ArtifactMetadata]:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=32,
            rightMargin=32,
            topMargin=32,
            bottomMargin=32
        )

        styles = getSampleStyleSheet()
        
        title_style = ParagraphStyle(
            "DocTitle",
            parent=styles["Heading1"],
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#0F172A"),
            spaceAfter=2
        )
        subtitle_style = ParagraphStyle(
            "DocSubtitle",
            parent=styles["Normal"],
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#64748B"),
            spaceAfter=10
        )
        h2_style = ParagraphStyle(
            "SectionH2",
            parent=styles["Heading2"],
            fontSize=10.5,
            leading=14,
            textColor=colors.HexColor("#1E293B"),
            spaceBefore=8,
            spaceAfter=4
        )
        body_style = ParagraphStyle(
            "BodyDark",
            parent=styles["Normal"],
            fontSize=7.5,
            leading=9.5,
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
        small_muted = ParagraphStyle(
            "SmallMuted",
            parent=body_style,
            fontSize=6.5,
            leading=8.5,
            textColor=colors.HexColor("#64748B")
        )
        partial_banner_style = ParagraphStyle(
            "PartialBanner",
            parent=body_style,
            fontSize=7,
            leading=9,
            textColor=colors.HexColor("#92400E"),
            fontName="Helvetica-Bold"
        )

        story = []

        # Header Title Banner
        story.append(Paragraph("EXECUTIVE PROCUREMENT AWARD REPORT", title_style))
        story.append(Paragraph(f"Authoritative Award Evaluation Baseline &bull; Project: {vm.rfq_title} ({vm.rfq_id})", subtitle_style))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2563EB"), spaceAfter=8))

        # A. Award Summary Metadata Grid
        status_color = "#10B981" if vm.is_finalized else "#F59E0B"
        meta_data = [
            [
                Paragraph("<b>Award ID:</b>", body_style), Paragraph(vm.award_id, body_style),
                Paragraph("<b>Award Status:</b>", body_style), Paragraph(f"<b><font color='{status_color}'>{vm.status}</font></b>", bold_style)
            ],
            [
                Paragraph("<b>RFQ Reference:</b>", body_style), Paragraph(vm.rfq_id, body_style),
                Paragraph("<b>Award Baseline Date:</b>", body_style), Paragraph(vm.finalized_at_display, body_style)
            ],
            [
                Paragraph("<b>Total Commitment:</b>", body_style), Paragraph(f"<b>{vm.total_awarded_value_display}</b>", bold_style),
                Paragraph("<b>Evaluated By:</b>", body_style), Paragraph(vm.buyer_id, body_style)
            ],
            [
                Paragraph("<b>Sourcing Strategy:</b>", body_style), Paragraph(vm.sourcing_strategy_label, body_style),
                Paragraph("<b>Comparison Baseline:</b>", body_style), Paragraph(vm.comparison_id, body_style)
            ]
        ]

        meta_table = Table(meta_data, colWidths=[110, 155, 115, 150])
        meta_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 8))

        # B. Fulfillment Summary Card
        ful = vm.fulfillment
        ful_data = [
            [
                Paragraph(f"<b>RFQ Unique Lines:</b> {ful.total_rfq_lines}", body_style),
                Paragraph(f"<b>Fully Fulfilled:</b> <font color='#10B981'>{ful.fully_allocated_lines}</font>", body_style),
                Paragraph(f"<b>Partially Fulfilled:</b> <font color='#F59E0B'>{ful.partially_allocated_lines}</font>", body_style),
                Paragraph(f"<b>Unallocated:</b> <font color='#EF4444'>{ful.unallocated_lines}</font>", body_style),
                Paragraph(f"<b>Overall Scope:</b> <b>{ful.overall_fulfillment_display}</b>", bold_style),
            ]
        ]
        ful_table = Table(ful_data, colWidths=[105, 105, 110, 100, 110])
        ful_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EFF6FF")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#BFDBFE")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DBEAFE")),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ]))
        story.append(ful_table)
        story.append(Spacer(1, 8))

        # C. Supplier Summary Table (Dimension-Safe, Unique RFQ Lines)
        story.append(Paragraph("1. Supplier Award Allocation Summary", h2_style))
        supp_headers = [
            Paragraph("Awarded Supplier", header_table_style),
            Paragraph("Quote Ref", header_table_style),
            Paragraph("RFQ Lines", header_table_style),
            Paragraph("Splits", header_table_style),
            Paragraph("Quantities Awarded (by UOM)", header_table_style),
            Paragraph(f"Award Value ({vm.base_currency})", header_table_style),
            Paragraph("Share", header_table_style)
        ]
        supp_table_data = [supp_headers]
        for s in vm.suppliers:
            supp_table_data.append([
                Paragraph(s.supplier_name, bold_style),
                Paragraph(s.quote_id.split("-")[-1] if len(s.quote_id) > 14 else s.quote_id, small_muted),
                Paragraph(str(s.rfq_lines_awarded_count), body_style),
                Paragraph(str(s.split_allocations_count), body_style),
                Paragraph(s.quantities_by_uom_display, body_style),
                Paragraph(s.total_value_display, bold_style),
                Paragraph(s.percentage_share_display, body_style)
            ])

        supp_table = Table(supp_table_data, colWidths=[125, 60, 45, 35, 130, 90, 45])
        supp_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(supp_table)
        story.append(Spacer(1, 8))

        # D. Line-Level Award Allocation Details (Grouped Parent Lines + Child Splits + Provenance)
        story.append(Paragraph("2. Line-Level Award Allocation & Provenance Details", h2_style))
        line_headers = [
            Paragraph("Line ID", header_table_style),
            Paragraph("Item / SKU Description", header_table_style),
            Paragraph("Required", header_table_style),
            Paragraph("Awarded Supplier / Allocation", header_table_style),
            Paragraph("Awarded Qty", header_table_style),
            Paragraph(f"Landed Cost ({vm.base_currency})", header_table_style),
            Paragraph(f"Extended ({vm.base_currency})", header_table_style)
        ]
        line_table_data = [line_headers]

        for line in vm.lines:
            if line.splits:
                num_splits = len(line.splits)
                for sp in line.splits:
                    is_first = (sp.split_index == 1)
                    
                    # SKU description with split indicator
                    sku_desc = f"<b>{line.item_sku}</b>"
                    if num_splits > 1:
                        sku_desc += f"<br/><font color='#64748B' size='6.5'>{sp.allocation_type_label}</font>"
                    if sp.commercial_breakdown_text:
                        sku_desc += f"<br/><font color='#475569' size='6'>[{sp.commercial_breakdown_text}]</font>"
                    if sp.uom_provenance_text:
                        sku_desc += f"<br/><font color='#2563EB' size='6'>[{sp.uom_provenance_text}]</font>"
                    
                    req_text = f"{line.required_qty_display} {line.canonical_uom}" if is_first else "<font color='#94A3B8'>&bull;</font>"
                    
                    # Stable Line ID column (75pt width prevents fragmented wrapping)
                    line_id_disp = line.line_id if is_first else ""

                    line_table_data.append([
                        Paragraph(line_id_disp, body_style),
                        Paragraph(sku_desc, body_style),
                        Paragraph(req_text, body_style),
                        Paragraph(sp.supplier_name, body_style),
                        Paragraph(f"{sp.awarded_qty_display} {sp.canonical_uom}", bold_style),
                        Paragraph(sp.unit_landed_cost_display, body_style),
                        Paragraph(sp.split_value_display, bold_style)
                    ])
                
                # If partial, append prominent partial fulfillment notice row
                if line.is_partial and line.partial_status_banner:
                    line_table_data.append([
                        Paragraph("", body_style),
                        Paragraph(line.partial_status_banner, partial_banner_style),
                        Paragraph("", body_style),
                        Paragraph("", body_style),
                        Paragraph("", body_style),
                        Paragraph("", body_style),
                        Paragraph("", body_style)
                    ])
            else:
                line_table_data.append([
                    Paragraph(line.line_id, body_style),
                    Paragraph(f"<b>{line.item_sku}</b><br/>{line.item_description}", body_style),
                    Paragraph(f"{line.required_qty_display} {line.canonical_uom}", body_style),
                    Paragraph("<font color='red'>UNALLOCATED</font>", body_style),
                    Paragraph("0", body_style),
                    Paragraph("0.00", body_style),
                    Paragraph("0.00", body_style)
                ])

        # Table Summary Row
        line_table_data.append([
            Paragraph("", body_style),
            Paragraph("<b>TOTAL COMMITMENT</b>", bold_style),
            Paragraph("", body_style),
            Paragraph("", body_style),
            Paragraph("", body_style),
            Paragraph("", body_style),
            Paragraph(f"<b>{vm.total_awarded_value_display}</b>", bold_style)
        ])

        line_table = Table(line_table_data, colWidths=[75, 140, 55, 110, 55, 45, 50])
        line_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]))
        story.append(line_table)

        doc.build(story)
        pdf_bytes = buf.getvalue()

        meta = ArtifactMetadata(
            artifact_id=f"ART-PDF-{vm.award_id}",
            award_id=vm.award_id,
            rfq_id=vm.rfq_id,
            artifact_type=ArtifactType.AWARD_REPORT,
            format=ArtifactFormat.PDF,
            name="Executive Award Report",
            filename=f"{vm.rfq_id}_Executive_Award_Report.pdf",
            mime_type="application/pdf",
            size_bytes=len(pdf_bytes),
            download_url=f"/rfqs/{vm.rfq_id}/award/export/pdf"
        )

        return pdf_bytes, meta


register_renderer(ArtifactType.AWARD_REPORT, AwardReportRenderer)
