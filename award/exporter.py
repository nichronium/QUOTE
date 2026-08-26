"""
Procurement Execution Artifacts & Exporter Engine.
Delegates to the pluggable Domain-Agnostic Artifact Renderer framework.
"""

from typing import Optional

from app import services
from artifacts.models import ArtifactType
from artifacts.service import render_award_artifact
from award.service import award_dict_to_canonical_record


def generate_award_excel_workbook(rfq_id: str) -> bytes:
    """
    Generates the authoritative Multi-Tab Excel Award Workbook via Artifact Renderer.
    """
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    award_dict = services.get_award_decision(rfq_id)
    if not award_dict:
        comp = services.get_latest_rfq_comparison(rfq_id)
        if not comp:
            raise ValueError(f"No comparison or award decision available for RFQ {rfq_id}.")
        award_dict = services.build_proposed_award_allocation(rfq_id)

    award_record = award_dict_to_canonical_record(award_dict)
    content, _ = render_award_artifact(award_record, ArtifactType.AWARD_WORKBOOK, rfq_title=rfq.title or rfq.rfq_id)
    return content


def generate_award_executive_pdf(rfq_id: str) -> bytes:
    """
    Generates the Executive Award Report PDF via Artifact Renderer.
    """
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    award_dict = services.get_award_decision(rfq_id)
    if not award_dict:
        comp = services.get_latest_rfq_comparison(rfq_id)
        if not comp:
            raise ValueError(f"No comparison or award decision available for RFQ {rfq_id}.")
        award_dict = services.build_proposed_award_allocation(rfq_id)

    award_record = award_dict_to_canonical_record(award_dict)
    content, _ = render_award_artifact(award_record, ArtifactType.AWARD_REPORT, rfq_title=rfq.title or rfq.rfq_id)
    return content


def generate_award_csv_export(rfq_id: str) -> str:
    """
    Generates the Flat CSV Procurement Allocation Export via Artifact Renderer.
    """
    rfq = services.get_rfq(rfq_id)
    if not rfq:
        raise ValueError(f"RFQ {rfq_id} not found.")

    award_dict = services.get_award_decision(rfq_id)
    if not award_dict:
        comp = services.get_latest_rfq_comparison(rfq_id)
        if not comp:
            raise ValueError(f"No comparison or award decision available for RFQ {rfq_id}.")
        award_dict = services.build_proposed_award_allocation(rfq_id)

    award_record = award_dict_to_canonical_record(award_dict)
    content_bytes, _ = render_award_artifact(award_record, ArtifactType.ERP_EXPORT, rfq_title=rfq.title or rfq.rfq_id)
    return content_bytes.decode("utf-8")
