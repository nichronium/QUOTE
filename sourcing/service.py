"""
High-Level Sourcing Optimization Service.
Orchestrates comparison matrix, hard constraint filters, and multi-scenario optimization.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from comparison.models import RFQComparison
from matching.models import SupplierMasterRecord
from sourcing.constraints import ConstraintEvaluator
from sourcing.models import (
    ProcurementStrategy,
    SourcingOptimizationReport,
    SourcingScenario,
)
from sourcing.optimizer import SourcingOptimizer
from sourcing.strategy import get_strategy_description


def run_sourcing_optimization(
    comparison: Any,
    strategy: ProcurementStrategy = ProcurementStrategy.COST_OPTIMIZED,
    supplier_masters: Optional[Dict[str, SupplierMasterRecord]] = None,
) -> SourcingOptimizationReport:
    """
    Executes enterprise sourcing optimization on a commercial comparison matrix.
    Generates ranked feasible scenarios, explainable recommendations, and audit evidence.
    """
    # 1. Parse into RFQComparison model if dict
    if isinstance(comparison, dict):
        comp_dict = comparison.get("comparison") or comparison
        comp_id = comparison.get("comparison_id") or comp_dict.get("comparison_id", f"COMP-{comp_dict.get('rfq_id', 'UNKNOWN')}")
        base_cur = comp_dict.get("base_currency", "INR")
        rfq_id = comp_dict.get("rfq_id", "")
        # Parse into RFQComparison
        try:
            comparison_obj = RFQComparison.model_validate(comp_dict)
        except Exception:
            comparison_obj = RFQComparison.parse_obj(comp_dict)
    else:
        comparison_obj = comparison
        comp_id = getattr(comparison, "comparison_id", None) or f"COMP-{getattr(comparison, 'rfq_id', 'UNKNOWN')}"
        base_cur = getattr(comparison, "base_currency", "INR")
        rfq_id = getattr(comparison, "rfq_id", "")

    optimizer = SourcingOptimizer()
    evaluator = ConstraintEvaluator()

    # 2. Run optimization pipeline
    ranked_scenarios: List[SourcingScenario] = optimizer.optimize(
        comparison=comparison_obj,
        strategy=strategy,
        supplier_masters=supplier_masters,
    )

    # 3. Extract ineligible suppliers across lines
    evaluations = evaluator.evaluate_all(comparison_obj, supplier_masters)
    ineligible_dict: Dict[str, List[str]] = {}
    for rfq_line_id, supp_evals in evaluations.items():
        for supp_id, ev in supp_evals.items():
            if not ev.is_eligible:
                if supp_id not in ineligible_dict:
                    ineligible_dict[supp_id] = []
                for reason in ev.violation_reasons:
                    if reason not in ineligible_dict[supp_id]:
                        ineligible_dict[supp_id].append(reason)

    # 4. Check evidence availability across the dataset
    has_any_lead_time = False
    has_any_spec_match = False
    has_any_payment_terms = False

    for item_comp in comparison_obj.item_comparisons.values():
        for p in item_comp.supplier_prices.values():
            if p.lead_time_days is not None:
                has_any_lead_time = True
            if p.matched_quote_item and hasattr(p.matched_quote_item, "candidate_item") and p.matched_quote_item.candidate_item:
                has_any_spec_match = True

    for supp in comparison_obj.suppliers.values():
        if supp.commercial_terms and (supp.commercial_terms.credit_days or supp.commercial_terms.advance_pct):
            has_any_payment_terms = True

    evidence_matrix = {
        "unit_landed_cost": True,
        "quoted_capacity": True,
        "lead_time_days": has_any_lead_time,
        "specification_match": has_any_spec_match,
        "commercial_payment_terms": has_any_payment_terms,
        "supplier_quality_rating": False,  # Not present in schema, explicitly False
    }

    # 5. Partition recommended vs alternatives
    recommended = ranked_scenarios[0] if ranked_scenarios else None
    alternatives = ranked_scenarios[1:] if len(ranked_scenarios) > 1 else []

    # 6. Compute line-level authoritative recommendations and alternatives
    line_recs = optimizer.optimize_all_lines(
        comparison=comparison_obj,
        strategy=strategy,
        supplier_masters=supplier_masters,
    )

    return SourcingOptimizationReport(
        rfq_id=rfq_id,
        comparison_id=comp_id,
        base_currency=base_cur,
        strategy=strategy,
        strategy_description=get_strategy_description(strategy),
        recommended_scenario=recommended,
        alternative_scenarios=alternatives,
        all_scenarios=ranked_scenarios,
        line_recommendations=line_recs,
        ineligible_suppliers=ineligible_dict,
        evidence_availability=evidence_matrix,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
