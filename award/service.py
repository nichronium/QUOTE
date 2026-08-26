"""
Post-Award Business Logic & Canonical Award Record Service.
"""

from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from award.models import (
    AwardCommercialSummary,
    AwardFulfillmentSummary,
    AwardRecord,
    AwardStatus,
    BuyerDecision,
    BuyerDecisionState,
    CommercialBreakdown,
    DecisionProvenance,
    LineAllocationState,
    LineAward,
    RecommendationSnapshot,
    SupplierAllocation,
    SupplierAwardSummary,
)


def award_dict_to_canonical_record(award_dict: Dict[str, Any]) -> AwardRecord:
    """
    Transforms an internal award dictionary into a strongly-typed, canonical AwardRecord.
    Reuses existing calculations without recalculating.
    Enforces dimension-safe multi-UOM accounting and unique RFQ line count semantics.
    """
    rfq_id = award_dict.get("rfq_id", "")
    award_id = award_dict.get("award_id", f"AWD-{rfq_id}")
    comp_id = award_dict.get("comparison_id", "")
    status_str = award_dict.get("status", "DRAFT")
    
    # Line awards
    line_awards: List[LineAward] = []
    supplier_agg: Dict[str, Dict[str, Any]] = {}
    
    unique_rfq_lines_count = 0
    fully_alloc_count = 0
    partially_alloc_count = 0
    unallocated_count = 0
    total_required_sum = Decimal("0")
    total_awarded_sum = Decimal("0")

    for a in award_dict.get("allocations", []):
        line_id = a.get("rfq_line_id", "")
        sku = a.get("item_sku", "")
        desc = a.get("item_description", "")
        raw_req = a.get("required_qty")
        req_q = Decimal(str(raw_req)) if raw_req is not None and str(raw_req).strip() not in ["", "None", "null"] else None
        uom = a.get("required_uom", "PCS")
        
        unique_rfq_lines_count += 1
        splits: List[SupplierAllocation] = []
        line_awarded_total = Decimal("0")

        for sp in a.get("supplier_splits", []):
            sid = sp.get("supplier_id", "")
            sname = sp.get("supplier_name", sid)
            qid = sp.get("quote_id", sid)
            aqty = Decimal(str(sp.get("allocated_qty", 0)))
            landed = Decimal(str(sp.get("unit_landed_cost", 0)))
            sval = Decimal(str(sp.get("split_value", aqty * landed)))
            line_awarded_total += aqty
            
            # Commercial breakdown
            lpb = sp.get("landed_price_breakdown", {})
            cb = CommercialBreakdown(
                base_unit_price=Decimal(str(sp.get("base_unit_price"))) if sp.get("base_unit_price") is not None else None,
                discount_pct=Decimal(str(sp.get("discount_pct", 0))) if sp.get("discount_pct") else None,
                tax_rate_pct=Decimal(str(sp.get("tax_rate_pct", 0))) if sp.get("tax_rate_pct") else None,
                tax_amount=Decimal(str(sp.get("tax_amount", 0))) if sp.get("tax_amount") else None,
                allocated_charges=Decimal(str(sp.get("allocated_charges", 0))) if sp.get("allocated_charges") else None,
                unit_landed_cost=landed,
                uom_conversion_factor=Decimal(str(sp.get("uom_conversion_factor", 1.0))),
                uom_conversion_formula=sp.get("uom_conversion_formula")
            )
            
            raw_sq_q = sp.get("supplier_quoted_qty")
            sq_q = Decimal(str(raw_sq_q)) if raw_sq_q is not None and str(raw_sq_q).strip() not in ["", "None", "null"] else None
            
            raw_conv = sp.get("converted_capacity")
            conv_cap = Decimal(str(raw_conv)) if raw_conv is not None and str(raw_conv).strip() not in ["", "None", "null"] else None

            splits.append(SupplierAllocation(
                supplier_id=sid,
                supplier_name=sname,
                quote_id=qid,
                awarded_qty=aqty,
                canonical_uom=uom,
                supplier_quoted_qty=sq_q,
                supplier_quoted_uom=sp.get("supplier_quoted_uom", uom),
                converted_capacity=conv_cap,
                unused_quote_qty=Decimal(str(sp.get("unused_quote_qty", 0))) if sp.get("unused_quote_qty") is not None else None,
                unused_converted_qty=Decimal(str(sp.get("unused_converted_qty", 0))) if sp.get("unused_converted_qty") is not None else None,
                unit_landed_cost=landed,
                award_value=sval,
                is_l1_for_line=bool(sp.get("is_l1_for_line", False)),
                commercial_breakdown=cb,
                uom_conversion_factor=Decimal(str(sp.get("uom_conversion_factor", 1.0))),
                uom_conversion_formula=sp.get("uom_conversion_formula")
            ))
            
            # Supplier aggregation (tracking unique RFQ lines, split allocations, and UOM-safe quantities)
            if sname not in supplier_agg:
                supplier_agg[sname] = {
                    "supplier_id": sid,
                    "supplier_name": sname,
                    "quote_id": qid,
                    "rfq_lines": set(),
                    "split_count": 0,
                    "quantities_by_uom": {},
                    "val": Decimal("0")
                }
            supplier_agg[sname]["rfq_lines"].add(line_id)
            supplier_agg[sname]["split_count"] += 1
            if uom not in supplier_agg[sname]["quantities_by_uom"]:
                supplier_agg[sname]["quantities_by_uom"][uom] = Decimal("0")
            supplier_agg[sname]["quantities_by_uom"][uom] += aqty
            supplier_agg[sname]["val"] += sval

        # Line classification
        if req_q is not None:
            total_required_sum += req_q
            total_awarded_sum += line_awarded_total
            if line_awarded_total >= req_q and req_q > 0:
                fully_alloc_count += 1
            elif line_awarded_total > 0:
                partially_alloc_count += 1
            else:
                unallocated_count += 1
        else:
            if line_awarded_total > 0:
                fully_alloc_count += 1
            else:
                unallocated_count += 1

        # Recommendation snapshot
        rec_data = a.get("recommendation", {})
        rec_snap = None
        if rec_data:
            rec_snap = RecommendationSnapshot(
                strategy=rec_data.get("strategy_type", "LINE_ITEM_OPTIMAL"),
                strategy_label=rec_data.get("strategy_label", "Cost Optimized"),
                total_recommended_value=Decimal(str(rec_data.get("total_line_value", 0))),
                fulfillment_pct=Decimal(str(rec_data.get("fulfillment_pct", 100.0))),
                is_supply_shortfall=bool(rec_data.get("is_supply_shortfall", False)),
                shortfall_qty=Decimal(str(rec_data.get("shortfall_qty", 0))),
                recommended_splits=rec_data.get("recommended_splits", []),
                alternatives=rec_data.get("alternatives", []),
                why_recommended=DecisionProvenance(
                    objective=rec_data.get("why_recommended_breakdown", {}).get("core_objective", "Lowest Total Landed Procurement Cost"),
                    strategy_label=rec_data.get("strategy_label", "Cost Optimized"),
                    feasibility_checks=rec_data.get("why_recommended_breakdown", {}).get("eligibility_checks", []),
                    allocation_rationale=rec_data.get("rationale")
                )
            )

        # Buyer decision
        b_status_str = a.get("buyer_decision_status") or "SYSTEM_RECOMMENDED"
        is_ack = (b_status_str in ["BUYER_ACCEPTED", "PARTIAL_ACKNOWLEDGED"] or award_dict.get("buyer_accepted_unallocated", False))
        b_dec = BuyerDecision(
            decision_state=BuyerDecisionState(b_status_str) if b_status_str in BuyerDecisionState.__members__ else BuyerDecisionState.SYSTEM_RECOMMENDED,
            buyer_id=award_dict.get("awarded_by", "Procurement Specialist"),
            recommendation_accepted=(b_status_str == "BUYER_ACCEPTED"),
            buyer_modified=(b_status_str == "BUYER_MODIFIED"),
            partial_acknowledged=is_ack
        )

        alloc_state_str = a.get("allocation_state", "UNALLOCATED")
        line_awards.append(LineAward(
            rfq_line_id=line_id,
            item_sku=sku,
            item_description=desc,
            required_qty=req_q,
            canonical_uom=uom,
            supplier_allocations=splits,
            awarded_qty=Decimal(str(a.get("total_allocated_qty", line_awarded_total))),
            remaining_qty=Decimal(str(a.get("remaining_qty", 0))) if a.get("remaining_qty") is not None else None,
            shortfall_qty=Decimal(str(a.get("shortfall_qty", 0))) if a.get("shortfall_qty") is not None else None,
            fulfillment_percentage=Decimal(str(a.get("fulfillment_pct", 0))),
            allocation_state=LineAllocationState(alloc_state_str) if alloc_state_str in LineAllocationState.__members__ else LineAllocationState.UNALLOCATED,
            recommendation_snapshot=rec_snap,
            buyer_decision=b_dec,
            partial_acknowledgement_acknowledged=is_ack,
            partial_acknowledgement_buyer=award_dict.get("awarded_by", "Procurement Specialist") if is_ack else None,
            partial_acknowledgement_timestamp=award_dict.get("awarded_at") if is_ack else None,
            total_line_value=Decimal(str(a.get("total_line_value", 0)))
        ))

    # Supplier Summary List
    tot_val = Decimal(str(award_dict.get("total_awarded_value", 0)))
    supp_summaries: List[SupplierAwardSummary] = []
    for sname, sdata in sorted(supplier_agg.items(), key=lambda x: x[1]["val"], reverse=True):
        share_pct = (sdata["val"] / tot_val * Decimal("100.0")) if tot_val > 0 else Decimal("0")
        supp_summaries.append(SupplierAwardSummary(
            supplier_id=sdata["supplier_id"],
            supplier_name=sdata["supplier_name"],
            quote_id=sdata["quote_id"],
            rfq_lines_awarded_count=len(sdata["rfq_lines"]),
            split_allocations_count=sdata["split_count"],
            quantities_by_uom=sdata["quantities_by_uom"],
            total_awarded_value=sdata["val"],
            percentage_share=share_pct
        ))

    # Commercial & Fulfillment summaries
    comm_summary = AwardCommercialSummary(
        base_currency=award_dict.get("base_currency", "INR"),
        total_awarded_value=tot_val
    )

    overall_pct = (total_awarded_sum / total_required_sum * Decimal("100.0")) if total_required_sum > 0 else Decimal("100.0")

    ful_summary = AwardFulfillmentSummary(
        total_rfq_lines=unique_rfq_lines_count,
        fully_allocated_lines_count=fully_alloc_count,
        partially_allocated_lines_count=partially_alloc_count,
        unallocated_lines_count=unallocated_count,
        overall_fulfillment_percentage=overall_pct,
        has_unallocated_quantities=(unallocated_count > 0 or partially_alloc_count > 0),
        buyer_accepted_unallocated=bool(award_dict.get("buyer_accepted_unallocated", False))
    )

    return AwardRecord(
        award_id=award_id,
        rfq_id=rfq_id,
        comparison_id=comp_id,
        status=AwardStatus(status_str) if status_str in AwardStatus.__members__ else AwardStatus.DRAFT,
        created_at=award_dict.get("created_at") or award_dict.get("awarded_at") or "",
        finalized_at=award_dict.get("awarded_at"),
        buyer_id=award_dict.get("awarded_by", "Procurement Specialist"),
        sourcing_strategy=award_dict.get("selected_scenario", "LINE_ITEM_OPTIMAL"),
        line_awards=line_awards,
        supplier_summary=supp_summaries,
        commercial_summary=comm_summary,
        fulfillment_summary=ful_summary,
        award_notes=award_dict.get("award_notes"),
        reopen_history=award_dict.get("reopen_history", [])
    )
