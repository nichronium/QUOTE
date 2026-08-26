"""
Domain-Agnostic View Model for Post-Award Artifact Renderers.
Prepares all string formatting, currency conversions, decimal alignments, and split indicators
so that renderers (PDF, Excel, CSV) are purely presentation projections with NO business calculations.
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from award.models import AwardRecord, LineAward, SupplierAllocation


class SplitItemViewModel(BaseModel):
    split_index: int
    total_splits: int
    is_single: bool
    supplier_id: str
    supplier_name: str
    quote_id: str
    quote_id_short: str
    awarded_qty_display: str
    canonical_uom: str
    unit_landed_cost_display: str
    split_value_display: str
    is_l1: bool
    allocation_type_label: str
    
    # Provenance
    uom_provenance_text: Optional[str] = None
    commercial_breakdown_text: Optional[str] = None
    
    # Detailed commercial breakdown components (only if present)
    base_unit_price_display: Optional[str] = None
    discount_display: Optional[str] = None
    tax_display: Optional[str] = None
    freight_display: Optional[str] = None


class LineItemViewModel(BaseModel):
    line_id: str
    item_sku: str
    item_description: str
    required_qty_display: str
    canonical_uom: str
    allocation_state: str
    fulfillment_pct_display: str
    total_awarded_qty_display: str
    total_line_value_display: str
    has_shortfall: bool
    shortfall_display: Optional[str] = None
    
    # Partial award provenance
    is_partial: bool = False
    partial_status_banner: Optional[str] = None
    
    # Recommendation vs Buyer Decision
    system_rec_summary: Optional[str] = None
    buyer_decision_summary: Optional[str] = None
    is_buyer_modified: bool = False
    decision_state_label: str = "System Recommended"
    
    splits: List[SplitItemViewModel] = Field(default_factory=list)


class SupplierSummaryViewModel(BaseModel):
    supplier_id: str
    supplier_name: str
    quote_id: str
    rfq_lines_awarded_count: int
    split_allocations_count: int
    quantities_by_uom_display: str
    total_value_display: str
    percentage_share_display: str


class FulfillmentSummaryViewModel(BaseModel):
    total_rfq_lines: int
    fully_allocated_lines: int
    partially_allocated_lines: int
    unallocated_lines: int
    overall_fulfillment_display: str


class AwardRecordViewModel(BaseModel):
    award_id: str
    rfq_id: str
    rfq_title: str
    comparison_id: str
    status: str
    is_finalized: bool
    created_at_display: str
    finalized_at_display: str
    buyer_id: str
    sourcing_strategy_label: str
    base_currency: str
    total_awarded_value_display: str
    
    fulfillment: FulfillmentSummaryViewModel
    suppliers: List[SupplierSummaryViewModel] = Field(default_factory=list)
    lines: List[LineItemViewModel] = Field(default_factory=list)
    reopen_history: List[Dict[str, Any]] = Field(default_factory=list)
    exceptions: List[str] = Field(default_factory=list)


def build_award_view_model(award: AwardRecord, rfq_title: str = "") -> AwardRecordViewModel:
    """
    Transforms canonical AwardRecord into a domain-agnostic presentation view model.
    No calculations are delegated to the renderers.
    """
    base_cur = award.commercial_summary.base_currency or "INR"
    tot_val = award.commercial_summary.total_awarded_value
    
    # Format line items & splits
    line_vms: List[LineItemViewModel] = []
    for line in award.line_awards:
        req_q = line.required_qty
        req_display = f"{req_q:,.2f}".rstrip("0").rstrip(".") if req_q is not None else "N/A"
        alloc_q = line.awarded_qty
        alloc_display = f"{alloc_q:,.2f}".rstrip("0").rstrip(".")
        line_val = line.total_line_value
        pct = line.fulfillment_percentage
        
        has_shortfall = (line.shortfall_qty is not None and line.shortfall_qty > Decimal("0"))
        shortfall_disp = f"{line.shortfall_qty:,.2f}".rstrip("0").rstrip(".") if has_shortfall else None
        is_partial = has_shortfall or (line.allocation_state.value in ["PARTIALLY_ALLOCATED", "PARTIALLY_FULFILLED"])
        
        # Partial status banner
        part_banner = None
        if is_partial:
            if line.partial_acknowledgement_acknowledged:
                by = line.partial_acknowledgement_buyer or award.buyer_id
                part_banner = f"PARTIAL AWARD CONFIRMED &bull; Buyer: {by} &bull; Required: {req_display} {line.canonical_uom} &bull; Awarded: {alloc_display} {line.canonical_uom} (Shortfall: {shortfall_disp} {line.canonical_uom})"
            else:
                part_banner = f"PARTIAL FULFILLMENT &bull; Required: {req_display} {line.canonical_uom} &bull; Awarded: {alloc_display} {line.canonical_uom} (Shortfall: {shortfall_disp} {line.canonical_uom})"

        # Recommendation vs Buyer Decision
        rec_snap = line.recommendation_snapshot
        rec_text = None
        if rec_snap:
            rec_splits_str = ", ".join(f"{s.get('supplier_name', s.get('supplier_id'))}: {s.get('awarded_qty')} {line.canonical_uom}" for s in rec_snap.recommended_splits)
            rec_text = f"Recommended ({rec_snap.strategy_label}): {rec_splits_str} (₹{rec_snap.total_recommended_value:,.2f})"

        b_dec = line.buyer_decision
        b_text = None
        if line.supplier_allocations:
            b_splits_str = ", ".join(f"{s.supplier_name}: {s.awarded_qty:,.2f}".rstrip("0").rstrip(".") + f" {line.canonical_uom}" for s in line.supplier_allocations)
            b_text = f"Final Allocation: {b_splits_str} (₹{line.total_line_value:,.2f})"

        dec_label = "System Recommended"
        if b_dec.buyer_modified:
            dec_label = "Buyer Modified"
        elif b_dec.recommendation_accepted:
            dec_label = "Buyer Accepted"
        elif b_dec.partial_acknowledged:
            dec_label = "Partial Acknowledged"

        split_vms: List[SplitItemViewModel] = []
        num_splits = len(line.supplier_allocations)
        
        for idx, sp in enumerate(line.supplier_allocations, start=1):
            sp_q = sp.awarded_qty
            sp_q_disp = f"{sp_q:,.2f}".rstrip("0").rstrip(".")
            sp_landed = sp.unit_landed_cost
            sp_val = sp.award_value
            
            alloc_type_lbl = f"Split Sourcing ({idx}/{num_splits})" if num_splits > 1 else "Single Sourcing"
            
            # Shortened quote reference for clean table presentation
            qid = sp.quote_id
            qid_short = qid.split("-")[-1] if "-" in qid and len(qid) > 16 else qid
            
            # Cross-UOM provenance text
            uom_prov = None
            if sp.uom_conversion_formula:
                uom_prov = f"UOM Conversion: {sp.uom_conversion_formula}"
            elif sp.supplier_quoted_uom and sp.supplier_quoted_uom != line.canonical_uom and sp.supplier_quoted_qty:
                uom_prov = f"Quoted: {sp.supplier_quoted_qty} {sp.supplier_quoted_uom} &rarr; Converted: {sp.converted_capacity or sp_q} {line.canonical_uom}"
            
            # Commercial components text
            cb = sp.commercial_breakdown
            comm_parts = []
            base_p = None
            disc = None
            tax = None
            freight = None
            
            if cb:
                if cb.base_unit_price is not None:
                    base_p = f"{base_cur} {cb.base_unit_price:,.2f}"
                    comm_parts.append(f"Base: {base_p}")
                if cb.discount_pct and cb.discount_pct > Decimal("0"):
                    disc = f"-{cb.discount_pct}%"
                    comm_parts.append(f"Disc: {disc}")
                if cb.allocated_charges and cb.allocated_charges > Decimal("0"):
                    freight = f"+{base_cur} {cb.allocated_charges:,.2f}"
                    comm_parts.append(f"Charges: {freight}")
                if cb.tax_amount and cb.tax_amount > Decimal("0"):
                    tax = f"+{base_cur} {cb.tax_amount:,.2f} ({cb.tax_rate_pct}%)"
                    comm_parts.append(f"Tax: {tax}")
            
            comm_prov_text = " &bull; ".join(comm_parts) if comm_parts else None

            split_vms.append(SplitItemViewModel(
                split_index=idx,
                total_splits=num_splits,
                is_single=(num_splits == 1),
                supplier_id=sp.supplier_id,
                supplier_name=sp.supplier_name,
                quote_id=qid,
                quote_id_short=qid_short,
                awarded_qty_display=sp_q_disp,
                canonical_uom=line.canonical_uom,
                unit_landed_cost_display=f"{sp_landed:,.2f}",
                split_value_display=f"{sp_val:,.2f}",
                is_l1=sp.is_l1_for_line,
                allocation_type_label=alloc_type_lbl,
                uom_provenance_text=uom_prov,
                commercial_breakdown_text=comm_prov_text,
                base_unit_price_display=base_p,
                discount_display=disc,
                tax_display=tax,
                freight_display=freight
            ))
        
        line_vms.append(LineItemViewModel(
            line_id=line.rfq_line_id,
            item_sku=line.item_sku,
            item_description=line.item_description,
            required_qty_display=req_display,
            canonical_uom=line.canonical_uom,
            allocation_state=line.allocation_state.value if hasattr(line.allocation_state, "value") else str(line.allocation_state),
            fulfillment_pct_display=f"{pct:.1f}%",
            total_awarded_qty_display=alloc_display,
            total_line_value_display=f"{line_val:,.2f}",
            has_shortfall=has_shortfall,
            shortfall_display=shortfall_disp,
            is_partial=is_partial,
            partial_status_banner=part_banner,
            system_rec_summary=rec_text,
            buyer_decision_summary=b_text,
            is_buyer_modified=b_dec.buyer_modified,
            decision_state_label=dec_label,
            splits=split_vms
        ))
    
    # Format supplier summary (UOM-safe quantities, unique line counts)
    supp_vms: List[SupplierSummaryViewModel] = []
    for s in award.supplier_summary:
        uom_quantities_str = " &bull; ".join(f"{uom}: {qty:,.2f}".rstrip("0").rstrip(".") for uom, qty in s.quantities_by_uom.items()) if s.quantities_by_uom else "0"
        supp_vms.append(SupplierSummaryViewModel(
            supplier_id=s.supplier_id,
            supplier_name=s.supplier_name,
            quote_id=s.quote_id,
            rfq_lines_awarded_count=s.rfq_lines_awarded_count,
            split_allocations_count=s.split_allocations_count,
            quantities_by_uom_display=uom_quantities_str,
            total_value_display=f"{s.total_awarded_value:,.2f}",
            percentage_share_display=f"{s.percentage_share:.1f}%"
        ))
    
    ful = award.fulfillment_summary
    is_fin = (award.status == "FINALIZED" or str(award.status).endswith("FINALIZED"))
    finalized_disp = award.finalized_at if is_fin and award.finalized_at else "Pending Finalization"
    
    ful_vm = FulfillmentSummaryViewModel(
        total_rfq_lines=ful.total_rfq_lines,
        fully_allocated_lines=ful.fully_allocated_lines_count,
        partially_allocated_lines=ful.partially_allocated_lines_count,
        unallocated_lines=ful.unallocated_lines_count,
        overall_fulfillment_display=f"{ful.overall_fulfillment_percentage:.1f}%"
    )

    return AwardRecordViewModel(
        award_id=award.award_id,
        rfq_id=award.rfq_id,
        rfq_title=rfq_title or award.rfq_id,
        comparison_id=award.comparison_id,
        status=award.status.value if hasattr(award.status, "value") else str(award.status),
        is_finalized=is_fin,
        created_at_display=award.created_at[:19].replace("T", " ") if award.created_at else "",
        finalized_at_display=finalized_disp[:19].replace("T", " ") if finalized_disp != "Pending Finalization" else finalized_disp,
        buyer_id=award.buyer_id,
        sourcing_strategy_label=award.sourcing_strategy.replace("_", " ").title(),
        base_currency=base_cur,
        total_awarded_value_display=f"{base_cur} {tot_val:,.2f}",
        fulfillment=ful_vm,
        lines=line_vms,
        suppliers=supp_vms,
        reopen_history=award.reopen_history,
        exceptions=award.exception_summary
    )
