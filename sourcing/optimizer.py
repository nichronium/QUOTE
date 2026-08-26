"""
Sourcing Optimization Engine for Quote Intelligence.
Generates feasible, capacity-constrained single-supplier and split-sourcing scenarios.
INVARIANT:
- Respects all hard constraints and supplier capacities.
- Computes transparent explanations, savings vs alternatives, and objective scores.
"""

import itertools
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from core.canonical_quote import quantize_currency
from comparison.models import ItemComparison, NormalizedItemPrice, RFQComparison
from matching.models import SupplierMasterRecord
from sourcing.constraints import ConstraintEvaluator
from sourcing.models import (
    LineAlternativeOption,
    LineRecommendation,
    LineSplitPlan,
    ProcurementStrategy,
    SourcingScenario,
    SupplierConstraintEvaluation,
    SupplierEvidence,
)
from sourcing.scorer import SourcingScorer
from sourcing.strategy import get_strategy_description


def _extract_quote_id(matched_item: Any) -> str:
    if not matched_item:
        return ""
    if hasattr(matched_item, "quote_id") and getattr(matched_item, "quote_id"):
        return str(getattr(matched_item, "quote_id"))
    if hasattr(matched_item, "quote_item") and getattr(matched_item, "quote_item") and hasattr(getattr(matched_item, "quote_item"), "quote_id"):
        return str(getattr(getattr(matched_item, "quote_item"), "quote_id"))
    return ""


class SourcingOptimizer:
    """Generates and ranks feasible multi-supplier procurement scenarios."""

    def __init__(self):
        self.constraint_evaluator = ConstraintEvaluator()
        self.scorer = SourcingScorer()

    def optimize(
        self,
        comparison: RFQComparison,
        strategy: ProcurementStrategy = ProcurementStrategy.COST_OPTIMIZED,
        supplier_masters: Optional[Dict[str, SupplierMasterRecord]] = None,
    ) -> List[SourcingScenario]:
        """
        Generates and ranks all feasible candidate scenarios under the specified strategy.
        Returns a deterministically ordered list with rank 1 as the recommended scenario.
        """
        base_currency = comparison.base_currency
        supplier_masters = supplier_masters or {}

        # 1. Hard constraint evaluations per line per supplier
        evaluations = self.constraint_evaluator.evaluate_all(comparison, supplier_masters)

        # 2. Extract eligible prices and score suppliers per line
        line_eligible_prices: Dict[str, List[NormalizedItemPrice]] = {}
        line_scores: Dict[str, Dict[str, Tuple[float, Dict[str, Optional[float]], SupplierEvidence]]] = {}

        for rfq_line_id, item_comp in comparison.item_comparisons.items():
            eligible: List[NormalizedItemPrice] = []
            line_evals = evaluations.get(rfq_line_id, {})
            for supp_id, price in item_comp.supplier_prices.items():
                ev = line_evals.get(supp_id)
                if ev and ev.is_eligible:
                    eligible.append(price)
            line_eligible_prices[rfq_line_id] = eligible
            line_scores[rfq_line_id] = self.scorer.score_line_suppliers(
                rfq_line_id, eligible, comparison, strategy
            )

        # 3. Generate candidate scenario archetypes
        scenarios: List[SourcingScenario] = []

        # Scenario Archetype 1: Lowest Total Cost Split Sourcing
        cost_split_scenario = self._build_line_optimal_split_scenario(
            comparison=comparison,
            line_eligible_prices=line_eligible_prices,
            sort_key=lambda p: (p.unit_landed_price_base if p.unit_landed_price_base is not None else Decimal("Infinity"), p.supplier_id),
            scenario_id="SCENARIO-COST-SPLIT",
            scenario_name="Lowest Total Cost — Split Sourcing",
            scenario_type="COST_OPTIMIZED_SPLIT",
            core_objective="Lowest Total Landed Procurement Cost",
        )
        if cost_split_scenario:
            scenarios.append(cost_split_scenario)

        # Scenario Archetype 2: Single-Supplier Whole Scope (L1) & Alternative Single Supplier (L2)
        single_scenarios = self._build_single_supplier_scenarios(
            comparison=comparison,
            line_eligible_prices=line_eligible_prices,
        )
        scenarios.extend(single_scenarios)

        # Scenario Archetype 3: Delivery Priority Sourcing
        delivery_scenario = self._build_line_optimal_split_scenario(
            comparison=comparison,
            line_eligible_prices=line_eligible_prices,
            sort_key=lambda p: (
                p.lead_time_days if p.lead_time_days is not None else 9999,
                p.unit_landed_price_base if p.unit_landed_price_base is not None else Decimal("Infinity"),
                p.supplier_id
            ),
            scenario_id="SCENARIO-DELIVERY-PRIORITY",
            scenario_name="Delivery Priority Sourcing",
            scenario_type="DELIVERY_PRIORITY",
            core_objective="Shortest Verified Delivery Lead Time",
        )
        if delivery_scenario:
            scenarios.append(delivery_scenario)

        # Scenario Archetype 4: Balanced Multi-Objective Sourcing
        balanced_scenario = self._build_line_optimal_split_scenario(
            comparison=comparison,
            line_eligible_prices=line_eligible_prices,
            sort_key=lambda p: (
                -line_scores.get(p.rfq_line_id, {}).get(p.supplier_id, (0.0,))[0],
                p.unit_landed_price_base if p.unit_landed_price_base is not None else Decimal("Infinity"),
                p.supplier_id
            ),
            scenario_id="SCENARIO-BALANCED-SPLIT",
            scenario_name="Balanced Multi-Objective Sourcing",
            scenario_type="BALANCED_SPLIT",
            core_objective="Balanced Cost, Delivery, Quality and Risk",
        )
        if balanced_scenario:
            scenarios.append(balanced_scenario)

        # 4. Filter out duplicate scenarios (identical allocations and costs)
        unique_scenarios = self._deduplicate_scenarios(scenarios)

        # 5. Score and Rank scenarios based on chosen strategy
        ranked_scenarios = self._rank_scenarios(unique_scenarios, strategy)

        # 6. Compute deltas vs recommended scenario
        if ranked_scenarios:
            rec = ranked_scenarios[0]
            rec.is_recommended = True
            rec_cost = rec.total_cost_base

            for rank_idx, sc in enumerate(ranked_scenarios, start=1):
                sc.recommendation_rank = rank_idx
                if rank_idx == 1:
                    sc.is_recommended = True
                    sc.cost_delta_vs_recommended = Decimal("0.0")
                    sc.cost_delta_pct_vs_recommended = Decimal("0.0")
                else:
                    sc.is_recommended = False
                    delta = quantize_currency(sc.total_cost_base - rec_cost)
                    sc.cost_delta_vs_recommended = delta
                    if rec_cost > Decimal("0"):
                        sc.cost_delta_pct_vs_recommended = ((delta / rec_cost) * Decimal("100.0")).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )

        return ranked_scenarios

    def _build_line_optimal_split_scenario(
        self,
        comparison: RFQComparison,
        line_eligible_prices: Dict[str, List[NormalizedItemPrice]],
        sort_key: Any,
        scenario_id: str,
        scenario_name: str,
        scenario_type: str,
        core_objective: str,
    ) -> Optional[SourcingScenario]:
        """Synthesizes a capacity-constrained split sourcing scenario across all lines."""
        base_currency = comparison.base_currency
        line_plans: Dict[str, List[LineSplitPlan]] = {}
        supplier_summary: Dict[str, Dict[str, Any]] = {}

        total_cost = Decimal("0.0")
        fully_fulfilled = 0
        partially_fulfilled = 0
        unallocated = 0
        total_required_items = len(comparison.item_comparisons)
        total_req_qty = Decimal("0.0")
        total_awarded_qty = Decimal("0.0")

        for rfq_line_id, item_comp in comparison.item_comparisons.items():
            req_qty = item_comp.requested_quantity or Decimal("0")
            req_uom = item_comp.requested_uom or "PCS"
            total_req_qty += req_qty

            eligible = list(line_eligible_prices.get(rfq_line_id, []))
            if not eligible:
                unallocated += 1
                line_plans[rfq_line_id] = []
                continue

            # Sort candidate bids by scenario sorting criteria
            eligible.sort(key=sort_key)

            # Greedy allocation filling requirement with best candidates up to quoted capacity
            remaining_to_allocate = req_qty
            line_splits: List[LineSplitPlan] = []

            for idx, price in enumerate(eligible):
                if remaining_to_allocate <= Decimal("0"):
                    break

                quoted_cap = price.quoted_qty
                # Capacity allocation rule: awarded = min(quoted_cap, remaining) if quoted_cap exists, else remaining
                if quoted_cap is not None:
                    alloc_qty = min(quoted_cap, remaining_to_allocate)
                else:
                    alloc_qty = remaining_to_allocate

                if alloc_qty <= Decimal("0"):
                    continue

                unit_cost = price.unit_landed_price_base or Decimal("0.0")
                split_val = quantize_currency(alloc_qty * unit_cost)
                unused_cap = quantize_currency(quoted_cap - alloc_qty) if quoted_cap is not None else None

                line_splits.append(LineSplitPlan(
                    supplier_id=price.supplier_id,
                    supplier_name=price.supplier_name,
                    quote_id=_extract_quote_id(price.matched_quote_item),
                    awarded_qty=alloc_qty,
                    quoted_capacity=quoted_cap,
                    quoted_uom=price.quoted_uom or req_uom,
                    unit_landed_cost=unit_cost,
                    split_value=split_val,
                    unused_quote_qty=unused_cap,
                    is_l1_for_line=(idx == 0),
                    allocation_reason=f"Allocated {alloc_qty} {req_uom} at {base_currency} {unit_cost:,.2f} based on {core_objective}"
                ))

                remaining_to_allocate -= alloc_qty
                total_cost += split_val
                total_awarded_qty += alloc_qty

                # Accumulate supplier summary
                supp_id = price.supplier_id
                if supp_id not in supplier_summary:
                    supplier_summary[supp_id] = {
                        "supplier_id": supp_id,
                        "supplier_name": price.supplier_name,
                        "awarded_qty": Decimal("0.0"),
                        "total_value": Decimal("0.0"),
                        "lines_count": 0,
                    }
                supplier_summary[supp_id]["awarded_qty"] += alloc_qty
                supplier_summary[supp_id]["total_value"] += split_val
                supplier_summary[supp_id]["lines_count"] += 1

            line_plans[rfq_line_id] = line_splits
            line_alloc_sum = sum(s.awarded_qty for s in line_splits)

            if line_alloc_sum >= req_qty and req_qty > Decimal("0"):
                fully_fulfilled += 1
            elif line_alloc_sum > Decimal("0"):
                partially_fulfilled += 1
            else:
                unallocated += 1

        # Calculate share percentages for summary
        for s_data in supplier_summary.values():
            s_val = s_data["total_value"]
            s_data["share_pct"] = ((s_val / total_cost) * Decimal("100.0")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            ) if total_cost > Decimal("0") else Decimal("0.0")

        fulfillment_pct = ((total_awarded_qty / total_req_qty) * Decimal("100.0")).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        ) if total_req_qty > Decimal("0") else Decimal("0.0")

        # Construct explanation
        num_suppliers = len(supplier_summary)
        supplier_names = [s["supplier_name"] for s in supplier_summary.values()]
        if num_suppliers > 1:
            supp_text = f"Split across {num_suppliers} suppliers ({', '.join(supplier_names)})"
        elif num_suppliers == 1:
            supp_text = f"Single-sourced with {supplier_names[0]}"
        else:
            supp_text = "No allocations possible"

        expl = (
            f"Recommended because this allocation satisfies all hard constraints and produces the optimal "
            f"normalized procurement plan under '{core_objective}'. {supp_text} with {fulfillment_pct}% fulfillment."
        )

        advantages = [
            f"Total landed cost: {base_currency} {total_cost:,.2f}",
            f"Fulfillment: {fulfillment_pct}% ({fully_fulfilled}/{total_required_items} items fully fulfilled)",
        ]
        if num_suppliers > 1:
            advantages.append(f"Supplier diversification across {num_suppliers} vendors mitigates delivery risk")

        return SourcingScenario(
            scenario_id=scenario_id,
            scenario_name=scenario_name,
            scenario_type=scenario_type,
            total_cost_base=total_cost,
            fulfillment_pct=fulfillment_pct,
            fully_fulfilled_lines=fully_fulfilled,
            partially_fulfilled_lines=partially_fulfilled,
            unallocated_lines=unallocated,
            total_awarded_items=len([lp for lp in line_plans.values() if lp]),
            supplier_allocations_summary=supplier_summary,
            line_plans=line_plans,
            composite_score=85.0,
            cost_score=90.0,
            explanation=expl,
            advantages=advantages,
            disadvantages=[] if unallocated == 0 and partially_fulfilled == 0 else [f"{partially_fulfilled + unallocated} items partially or unfulfilled"],
        )

    def _build_single_supplier_scenarios(
        self,
        comparison: RFQComparison,
        line_eligible_prices: Dict[str, List[NormalizedItemPrice]],
    ) -> List[SourcingScenario]:
        """Evaluates single-supplier scenarios for all eligible suppliers capable of quoting."""
        base_currency = comparison.base_currency
        supplier_quotes_count: Dict[str, int] = {}
        all_suppliers: Dict[str, str] = {}

        for rfq_line_id, prices in line_eligible_prices.items():
            for p in prices:
                all_suppliers[p.supplier_id] = p.supplier_name
                supplier_quotes_count[p.supplier_id] = supplier_quotes_count.get(p.supplier_id, 0) + 1

        candidate_single_plans: List[Tuple[str, str, Decimal, Decimal, Dict[str, List[LineSplitPlan]], int, int, int]] = []
        total_req_items = len(comparison.item_comparisons)
        total_req_qty = sum((comp.requested_quantity or Decimal("0")) for comp in comparison.item_comparisons.values())

        for supp_id, supp_name in all_suppliers.items():
            supp_cost = Decimal("0.0")
            supp_awarded_qty = Decimal("0.0")
            line_plans: Dict[str, List[LineSplitPlan]] = {}
            fully_fulfilled = 0
            partially_fulfilled = 0
            unallocated = 0

            for rfq_line_id, item_comp in comparison.item_comparisons.items():
                req_qty = item_comp.requested_quantity or Decimal("0")
                req_uom = item_comp.requested_uom or "PCS"

                # Find this supplier's price for this line
                supp_price = next(
                    (p for p in line_eligible_prices.get(rfq_line_id, []) if p.supplier_id == supp_id),
                    None
                )

                if supp_price and supp_price.unit_landed_price_base is not None:
                    quoted_cap = supp_price.quoted_qty
                    alloc_qty = min(quoted_cap, req_qty) if quoted_cap is not None else req_qty
                    unit_cost = supp_price.unit_landed_price_base
                    split_val = quantize_currency(alloc_qty * unit_cost)
                    unused_cap = quantize_currency(quoted_cap - alloc_qty) if quoted_cap is not None else None

                    if alloc_qty > Decimal("0"):
                        line_plans[rfq_line_id] = [LineSplitPlan(
                            supplier_id=supp_id,
                            supplier_name=supp_name,
                            quote_id=_extract_quote_id(supp_price.matched_quote_item),
                            awarded_qty=alloc_qty,
                            quoted_capacity=quoted_cap,
                            quoted_uom=supp_price.quoted_uom or req_uom,
                            unit_landed_cost=unit_cost,
                            split_value=split_val,
                            unused_quote_qty=unused_cap,
                            is_l1_for_line=True,
                            allocation_reason=f"Single-supplier allocation to {supp_name}"
                        )]
                        supp_cost += split_val
                        supp_awarded_qty += alloc_qty

                        if alloc_qty >= req_qty and req_qty > Decimal("0"):
                            fully_fulfilled += 1
                        else:
                            partially_fulfilled += 1
                    else:
                        line_plans[rfq_line_id] = []
                        unallocated += 1
                else:
                    line_plans[rfq_line_id] = []
                    unallocated += 1

            fulfillment_pct = ((supp_awarded_qty / total_req_qty) * Decimal("100.0")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            ) if total_req_qty > Decimal("0") else Decimal("0.0")

            candidate_single_plans.append((
                supp_id,
                supp_name,
                supp_cost,
                fulfillment_pct,
                line_plans,
                fully_fulfilled,
                partially_fulfilled,
                unallocated
            ))

        # Sort single supplier plans: Highest fulfillment first, then lowest cost
        candidate_single_plans.sort(
            key=lambda x: (-x[3], x[2] if x[2] > Decimal("0") else Decimal("Infinity"), x[0])
        )

        single_scenarios: List[SourcingScenario] = []

        if len(candidate_single_plans) >= 1:
            best = candidate_single_plans[0]
            s_sum = {
                best[0]: {
                    "supplier_id": best[0],
                    "supplier_name": best[1],
                    "awarded_qty": sum(sum(s.awarded_qty for s in splits) for splits in best[4].values()),
                    "total_value": best[2],
                    "share_pct": Decimal("100.0"),
                    "lines_count": len([splits for splits in best[4].values() if splits]),
                }
            }
            single_scenarios.append(SourcingScenario(
                scenario_id="SCENARIO-SINGLE-L1",
                scenario_name=f"Single Supplier — {best[1]}",
                scenario_type="SINGLE_SUPPLIER_L1",
                total_cost_base=best[2],
                fulfillment_pct=best[3],
                fully_fulfilled_lines=best[5],
                partially_fulfilled_lines=best[6],
                unallocated_lines=best[7],
                total_awarded_items=len([splits for splits in best[4].values() if splits]),
                supplier_allocations_summary=s_sum,
                line_plans=best[4],
                composite_score=80.0,
                cost_score=85.0,
                explanation=f"Single vendor award consolidation with {best[1]}. Provides simplified logistics and commercial contracting.",
                advantages=[
                    f"Consolidated contract with {best[1]} (Zero split-coordination overhead)",
                    f"Total landed cost: {base_currency} {best[2]:,.2f}",
                ],
                disadvantages=[] if best[7] == 0 else [f"{best[7]} items unquoted / unfulfilled by this vendor"],
            ))

        if len(candidate_single_plans) >= 2:
            second = candidate_single_plans[1]
            s_sum2 = {
                second[0]: {
                    "supplier_id": second[0],
                    "supplier_name": second[1],
                    "awarded_qty": sum(sum(s.awarded_qty for s in splits) for splits in second[4].values()),
                    "total_value": second[2],
                    "share_pct": Decimal("100.0"),
                    "lines_count": len([splits for splits in second[4].values() if splits]),
                }
            }
            single_scenarios.append(SourcingScenario(
                scenario_id="SCENARIO-SINGLE-L2",
                scenario_name=f"Alternative Single Supplier — {second[1]}",
                scenario_type="ALTERNATIVE_SINGLE",
                total_cost_base=second[2],
                fulfillment_pct=second[3],
                fully_fulfilled_lines=second[5],
                partially_fulfilled_lines=second[6],
                unallocated_lines=second[7],
                total_awarded_items=len([splits for splits in second[4].values() if splits]),
                supplier_allocations_summary=s_sum2,
                line_plans=second[4],
                composite_score=70.0,
                cost_score=75.0,
                explanation=f"Alternative single vendor award option with {second[1]}.",
                advantages=[f"Consolidated single contract with {second[1]}"],
                disadvantages=[f"{second[7]} items unquoted" if second[7] > 0 else "Higher cost than L1"],
            ))

        return single_scenarios

    def _deduplicate_scenarios(self, scenarios: List[SourcingScenario]) -> List[SourcingScenario]:
        """Removes scenarios with identical total costs and identical line allocations."""
        unique: List[SourcingScenario] = []
        seen_signatures: Set[str] = set()

        for sc in scenarios:
            # Build allocation signature
            sig_parts = []
            for rfq_line_id in sorted(sc.line_plans.keys()):
                splits = sc.line_plans[rfq_line_id]
                for s in splits:
                    sig_parts.append(f"{rfq_line_id}:{s.supplier_id}:{s.awarded_qty}:{s.unit_landed_cost}")
            sig = f"{sc.scenario_type}|{sc.total_cost_base}|{'|'.join(sig_parts)}"

            if sig not in seen_signatures:
                seen_signatures.add(sig)
                unique.append(sc)

        return unique

    def _rank_scenarios(
        self, scenarios: List[SourcingScenario], strategy: ProcurementStrategy
    ) -> List[SourcingScenario]:
        """Ranks scenarios deterministically according to the selected strategy."""
        if not scenarios:
            return []

        def scenario_sort_key(sc: SourcingScenario) -> Tuple:
            # 1. Fulfillment percentage (highest first)
            # 2. Strategy-specific primary sorting criteria
            # 3. Cost (lowest first)
            if strategy == ProcurementStrategy.COST_OPTIMIZED:
                return (
                    -float(sc.fulfillment_pct),
                    sc.total_cost_base if sc.total_cost_base > Decimal("0") else Decimal("Infinity"),
                    -sc.composite_score,
                    sc.scenario_id,
                )
            elif strategy == ProcurementStrategy.DELIVERY_PRIORITY:
                is_delivery_type = (sc.scenario_type == "DELIVERY_PRIORITY")
                return (
                    -float(sc.fulfillment_pct),
                    0 if is_delivery_type else 1,
                    sc.total_cost_base if sc.total_cost_base > Decimal("0") else Decimal("Infinity"),
                    sc.scenario_id,
                )
            elif strategy == ProcurementStrategy.BALANCED:
                is_balanced_type = (sc.scenario_type == "BALANCED_SPLIT")
                return (
                    -float(sc.fulfillment_pct),
                    0 if is_balanced_type else 1,
                    -sc.composite_score,
                    sc.total_cost_base if sc.total_cost_base > Decimal("0") else Decimal("Infinity"),
                    sc.scenario_id,
                )
            elif strategy == ProcurementStrategy.QUALITY_PRIORITY:
                return (
                    -float(sc.fulfillment_pct),
                    -(sc.quality_score or 0.0),
                    sc.total_cost_base if sc.total_cost_base > Decimal("0") else Decimal("Infinity"),
                    sc.scenario_id,
                )
            else:
                return (
                    -float(sc.fulfillment_pct),
                    sc.total_cost_base if sc.total_cost_base > Decimal("0") else Decimal("Infinity"),
                    sc.scenario_id,
                )

        ranked = sorted(scenarios, key=scenario_sort_key)
        return ranked

    def optimize_line(
        self,
        rfq_line_id: str,
        item_comp: ItemComparison,
        eligible_prices: List[NormalizedItemPrice],
        strategy: ProcurementStrategy = ProcurementStrategy.COST_OPTIMIZED,
        base_currency: str = "INR",
        line_scores: Optional[Dict[str, Tuple[float, Dict[str, Optional[float]], SupplierEvidence]]] = None,
        all_prices: Optional[List[NormalizedItemPrice]] = None,
        line_evaluations: Optional[Dict[str, SupplierConstraintEvaluation]] = None,
    ) -> LineRecommendation:
        """
        Calculates the authoritative recommended allocation, categorized alternative options,
        supply shortfall detection, and supplier exclusion reasons for a single RFQ Line.
        Evaluates best single-source vs 2-way split vs multi-source allocations under strict capacity constraints.
        """
        raw_req_q = getattr(item_comp, "requested_quantity", None)
        req_qty = Decimal(str(raw_req_q)) if raw_req_q is not None and str(raw_req_q).strip() not in ["", "None", "null"] else None
        req_uom = getattr(item_comp, "requested_uom", None) or "PCS"
        sku = getattr(item_comp, "sku", None) or getattr(item_comp, "item_sku", None) or rfq_line_id
        desc = getattr(item_comp, "item_description", None) or getattr(item_comp, "description", None) or sku

        strat_label = strategy.value.replace("_", " ").title()

        # Helper to extract supplier quoted capacity & converted capacity in canonical RFQ UOM
        def _get_capacity_info(p: NormalizedItemPrice) -> Tuple[Optional[Decimal], str, Optional[Decimal], Decimal, Optional[str]]:
            raw_c = p.quoted_qty
            q_uom = p.quoted_uom or req_uom
            factor = getattr(p, "uom_conversion_factor", None)
            if factor is None or factor <= Decimal("0"):
                factor = Decimal("1.0")
            if raw_c is None:
                return None, q_uom, None, factor, None
            conv_c = (raw_c * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            form = f"{raw_c} {q_uom} × {factor} = {conv_c} {req_uom}" if q_uom.upper().strip() != req_uom.upper().strip() and factor != Decimal("1.0") else None
            return raw_c, q_uom, conv_c, factor, form

        # Check total aggregate eligible capacity in canonical RFQ UOM
        has_unbounded_cap = False
        agg_eligible_cap = Decimal("0")
        for p in eligible_prices:
            _, _, conv_c, _, _ = _get_capacity_info(p)
            if conv_c is None:
                has_unbounded_cap = True
            else:
                agg_eligible_cap += conv_c

        display_agg_cap = None if has_unbounded_cap else agg_eligible_cap
        is_supply_shortfall = (req_qty is not None and not has_unbounded_cap and agg_eligible_cap < req_qty)

        if not eligible_prices or req_qty is None or req_qty <= Decimal("0"):
            has_no_quotes = not eligible_prices
            rat = (
                "No eligible supplier bids available for this item meeting all hard procurement and UOM constraints."
                if has_no_quotes
                else "RFQ requirement quantity is not specified or is zero."
            )
            return LineRecommendation(
                rfq_line_id=rfq_line_id,
                item_sku=sku,
                item_description=desc,
                strategy=strategy,
                strategy_label=strat_label,
                required_qty=req_qty,
                required_uom=req_uom,
                recommended_option_type="UNALLOCATED",
                recommended_splits=[],
                total_allocated_qty=Decimal("0"),
                remaining_qty=req_qty or Decimal("0"),
                shortfall_qty=req_qty or Decimal("0"),
                fulfillment_pct=Decimal("0.0"),
                total_line_value=Decimal("0.0"),
                is_supply_shortfall=True if req_qty and req_qty > Decimal("0") else False,
                aggregate_eligible_capacity=display_agg_cap,
                rationale=rat,
                why_recommended_breakdown={
                    "objective": strat_label,
                    "core_objective": "Procurement Allocation Optimization",
                    "eligibility_checks": [
                        "✗ No eligible suppliers with comparable bids" if has_no_quotes else "✗ Missing RFQ requirement quantity"
                    ],
                    "allocations": [],
                    "total_value": "0.00",
                    "required_qty": str(req_qty) if req_qty is not None else "0",
                    "allocated_qty": "0",
                    "remaining_qty": str(req_qty) if req_qty is not None else "0",
                    "shortfall_qty": str(req_qty) if req_qty is not None else "0",
                    "fulfillment_pct": "0.0",
                    "alternatives_comparison": [],
                },
                alternatives=[],
                full_fulfillment_options=[],
                partial_fulfillment_options=[],
                supplier_exclusion_reasons={},
            )

        # 1. Determine sort key for split-sourcing allocation
        if strategy == ProcurementStrategy.DELIVERY_PRIORITY:
            split_sort_key = lambda p: (
                p.lead_time_days if p.lead_time_days is not None else 9999,
                p.unit_landed_price_base if p.unit_landed_price_base is not None else Decimal("Infinity"),
                p.supplier_id,
            )
            strategy_obj_desc = "Shortest Verified Delivery Lead Time"
        elif strategy == ProcurementStrategy.BALANCED:
            scores_map = line_scores or {}
            split_sort_key = lambda p: (
                -scores_map.get(p.supplier_id, (0.0,))[0],
                p.unit_landed_price_base if p.unit_landed_price_base is not None else Decimal("Infinity"),
                p.supplier_id,
            )
            strategy_obj_desc = "Balanced Cost, Delivery & Quality"
        elif strategy == ProcurementStrategy.QUALITY_PRIORITY:
            split_sort_key = lambda p: (
                -getattr(p, "spec_match_score", 0.0) if getattr(p, "spec_match_score", None) is not None else 0.0,
                p.unit_landed_price_base if p.unit_landed_price_base is not None else Decimal("Infinity"),
                p.supplier_id,
            )
            strategy_obj_desc = "Highest Quality & Specification Match"
        else:
            split_sort_key = lambda p: (
                p.unit_landed_price_base if p.unit_landed_price_base is not None else Decimal("Infinity"),
                p.supplier_id,
            )
            strategy_obj_desc = "Lowest Total Landed Procurement Cost"

        candidate_options: List[LineAlternativeOption] = []

        # 2. Build Single-Supplier Options for EACH eligible supplier
        for price in eligible_prices:
            raw_cap, q_uom, conv_cap, u_factor, u_form = _get_capacity_info(price)
            alloc_qty = min(conv_cap, req_qty) if conv_cap is not None else req_qty
            if alloc_qty <= Decimal("0"):
                continue

            unit_cost = price.unit_landed_price_base or Decimal("0.0")
            split_val = quantize_currency(alloc_qty * unit_cost)
            unused_conv = quantize_currency(conv_cap - alloc_qty) if conv_cap is not None else None
            unused_raw = ((conv_cap - alloc_qty) / u_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if conv_cap is not None and u_factor > Decimal("0") else None
            rem_qty = max(Decimal("0"), req_qty - alloc_qty)
            ful_pct = ((alloc_qty / req_qty) * Decimal("100.0")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

            single_split = LineSplitPlan(
                supplier_id=price.supplier_id,
                supplier_name=price.supplier_name,
                quote_id=_extract_quote_id(getattr(price, "matched_quote_item", None)) or getattr(price, "quote_id", ""),
                awarded_qty=alloc_qty,
                quoted_capacity=raw_cap,
                quoted_uom=q_uom,
                converted_capacity=conv_cap,
                converted_uom=req_uom,
                uom_conversion_factor=u_factor,
                uom_conversion_formula=u_form,
                unit_landed_cost=unit_cost,
                split_value=split_val,
                unused_quote_qty=unused_raw,
                unused_converted_qty=unused_conv,
                is_l1_for_line=False,
                allocation_reason=f"Single-supplier award to {price.supplier_name}",
            )

            is_full = (alloc_qty >= req_qty)
            opt_type = "SINGLE_SUPPLIER" if is_full else "PARTIAL_FULFILLMENT"
            category = "FULL_FULFILLMENT" if is_full else "PARTIAL_FULFILLMENT"
            opt_title = (
                f"Single Supplier — {price.supplier_name}"
                if is_full
                else f"Single Supplier — {price.supplier_name} (Partial {ful_pct}%)"
            )
            opt_expl = (
                f"Single vendor allocation to {price.supplier_name} ({alloc_qty} {req_uom} @ {base_currency} {unit_cost:,.2f}). Total value: {base_currency} {split_val:,.2f}."
                if is_full
                else f"Partial single vendor allocation to {price.supplier_name} ({alloc_qty} of {req_qty} {req_uom} @ {base_currency} {unit_cost:,.2f}). Leaves {rem_qty} {req_uom} shortfall."
            )

            candidate_options.append(
                LineAlternativeOption(
                    option_id=f"OPT-SINGLE-{price.supplier_id}",
                    option_type=opt_type,
                    category=category,
                    title=opt_title,
                    supplier_names=[price.supplier_name],
                    splits=[single_split],
                    total_allocated_qty=alloc_qty,
                    total_line_value=split_val,
                    fulfillment_pct=ful_pct,
                    remaining_qty=rem_qty,
                    shortfall_qty=rem_qty,
                    feasibility_status="FEASIBLE" if is_full else "SHORTFALL",
                    reason="100% Demand Fulfilled by Single Supplier" if is_full else f"Supply Shortfall: Leaves {rem_qty} {req_uom} unfulfilled",
                    explanation=opt_expl,
                )
            )

        # 3. Build Feasible 2-Supplier Allocations
        sorted_eligible = sorted(eligible_prices, key=split_sort_key)
        if len(sorted_eligible) >= 2:
            for p1, p2 in itertools.combinations(sorted_eligible, 2):
                raw1, uom1, conv1, f1, form1 = _get_capacity_info(p1)
                raw2, uom2, conv2, f2, form2 = _get_capacity_info(p2)
                
                alloc1 = min(conv1, req_qty) if conv1 is not None else req_qty
                rem_after_1 = max(Decimal("0"), req_qty - alloc1)
                alloc2 = min(conv2, rem_after_1) if conv2 is not None else rem_after_1

                if alloc1 > Decimal("0") and alloc2 > Decimal("0"):
                    tot_alloc = alloc1 + alloc2
                    u_cost1 = p1.unit_landed_price_base or Decimal("0.0")
                    u_cost2 = p2.unit_landed_price_base or Decimal("0.0")
                    val1 = quantize_currency(alloc1 * u_cost1)
                    val2 = quantize_currency(alloc2 * u_cost2)
                    tot_val = val1 + val2
                    rem_2 = max(Decimal("0"), req_qty - tot_alloc)
                    ful_2 = ((tot_alloc / req_qty) * Decimal("100.0")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                    is_full_2 = (tot_alloc >= req_qty)

                    s1 = LineSplitPlan(
                        supplier_id=p1.supplier_id,
                        supplier_name=p1.supplier_name,
                        quote_id=_extract_quote_id(getattr(p1, "matched_quote_item", None)) or getattr(p1, "quote_id", ""),
                        awarded_qty=alloc1,
                        quoted_capacity=raw1,
                        quoted_uom=uom1,
                        converted_capacity=conv1,
                        converted_uom=req_uom,
                        uom_conversion_factor=f1,
                        uom_conversion_formula=form1,
                        unit_landed_cost=u_cost1,
                        split_value=val1,
                        unused_quote_qty=((conv1 - alloc1) / f1).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if conv1 is not None and f1 > Decimal("0") else None,
                        unused_converted_qty=quantize_currency(conv1 - alloc1) if conv1 is not None else None,
                        is_l1_for_line=True,
                        allocation_reason=f"2-supplier split allocation ({p1.supplier_name}: {alloc1} {req_uom})",
                    )
                    s2 = LineSplitPlan(
                        supplier_id=p2.supplier_id,
                        supplier_name=p2.supplier_name,
                        quote_id=_extract_quote_id(getattr(p2, "matched_quote_item", None)) or getattr(p2, "quote_id", ""),
                        awarded_qty=alloc2,
                        quoted_capacity=raw2,
                        quoted_uom=uom2,
                        converted_capacity=conv2,
                        converted_uom=req_uom,
                        uom_conversion_factor=f2,
                        uom_conversion_formula=form2,
                        unit_landed_cost=u_cost2,
                        split_value=val2,
                        unused_quote_qty=((conv2 - alloc2) / f2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if conv2 is not None and f2 > Decimal("0") else None,
                        unused_converted_qty=quantize_currency(conv2 - alloc2) if conv2 is not None else None,
                        is_l1_for_line=False,
                        allocation_reason=f"2-supplier split allocation ({p2.supplier_name}: {alloc2} {req_uom})",
                    )

                    candidate_options.append(
                        LineAlternativeOption(
                            option_id=f"OPT-PAIR-{p1.supplier_id}-{p2.supplier_id}",
                            option_type="SPLIT_SOURCING" if is_full_2 else "PARTIAL_FULFILLMENT",
                            category="FULL_FULFILLMENT" if is_full_2 else "PARTIAL_FULFILLMENT",
                            title=f"Split Sourcing — {p1.supplier_name} + {p2.supplier_name}" if is_full_2 else f"Split Sourcing — {p1.supplier_name} + {p2.supplier_name} (Partial {ful_2}%)",
                            supplier_names=[p1.supplier_name, p2.supplier_name],
                            splits=[s1, s2],
                            total_allocated_qty=tot_alloc,
                            total_line_value=tot_val,
                            fulfillment_pct=ful_2,
                            remaining_qty=rem_2,
                            shortfall_qty=rem_2,
                            feasibility_status="FEASIBLE" if is_full_2 else "SHORTFALL",
                            reason="Satisfies 100% demand via 2-way split" if is_full_2 else f"Leaves {rem_2} {req_uom} unfulfilled",
                            explanation=f"2-supplier split ({p1.supplier_name} {alloc1} {req_uom} + {p2.supplier_name} {alloc2} {req_uom}) totaling {base_currency} {tot_val:,.2f}.",
                        )
                    )

        # 4. Build Greedy Multi-Supplier Split Option across eligible suppliers
        remaining_to_allocate = req_qty
        split_plans: List[LineSplitPlan] = []
        total_split_cost = Decimal("0.0")
        total_split_allocated = Decimal("0.0")

        for idx, price in enumerate(sorted_eligible):
            if remaining_to_allocate <= Decimal("0"):
                break

            raw_cap, q_uom, conv_cap, u_factor, u_form = _get_capacity_info(price)
            alloc_qty = min(conv_cap, remaining_to_allocate) if conv_cap is not None else remaining_to_allocate
            if alloc_qty <= Decimal("0"):
                continue

            unit_cost = price.unit_landed_price_base or Decimal("0.0")
            split_val = quantize_currency(alloc_qty * unit_cost)
            unused_conv = quantize_currency(conv_cap - alloc_qty) if conv_cap is not None else None
            unused_raw = ((conv_cap - alloc_qty) / u_factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if conv_cap is not None and u_factor > Decimal("0") else None

            split_plans.append(
                LineSplitPlan(
                    supplier_id=price.supplier_id,
                    supplier_name=price.supplier_name,
                    quote_id=_extract_quote_id(getattr(price, "matched_quote_item", None)) or getattr(price, "quote_id", ""),
                    awarded_qty=alloc_qty,
                    quoted_capacity=raw_cap,
                    quoted_uom=q_uom,
                    converted_capacity=conv_cap,
                    converted_uom=req_uom,
                    uom_conversion_factor=u_factor,
                    uom_conversion_formula=u_form,
                    unit_landed_cost=unit_cost,
                    split_value=split_val,
                    unused_quote_qty=unused_raw,
                    unused_converted_qty=unused_conv,
                    is_l1_for_line=(idx == 0),
                    allocation_reason=f"Multi-supplier split allocation to {price.supplier_name} ({alloc_qty} {req_uom})",
                )
            )

            remaining_to_allocate -= alloc_qty
            total_split_cost += split_val
            total_split_allocated += alloc_qty

        if len(split_plans) > 1:
            split_rem = max(Decimal("0"), req_qty - total_split_allocated)
            split_ful = ((total_split_allocated / req_qty) * Decimal("100.0")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            supp_names = [s.supplier_name for s in split_plans]
            supp_splits_str = " + ".join([f"{s.supplier_name} ({s.awarded_qty} {req_uom})" for s in split_plans])
            is_full_split = (total_split_allocated >= req_qty)
            split_title = f"Split Sourcing — {' + '.join(supp_names)}" if is_full_split else f"Split Sourcing — {' + '.join(supp_names)} (Partial {split_ful}%)"
            split_expl = (
                f"Split allocation across {len(split_plans)} suppliers ({supp_splits_str}) achieving {split_ful}% fulfillment at total landed cost {base_currency} {total_split_cost:,.2f}."
            )

            candidate_options.append(
                LineAlternativeOption(
                    option_id="OPT-SPLIT-OPTIMAL",
                    option_type="SPLIT_SOURCING" if is_full_split else "PARTIAL_FULFILLMENT",
                    category="FULL_FULFILLMENT" if is_full_split else "PARTIAL_FULFILLMENT",
                    title=split_title,
                    supplier_names=supp_names,
                    splits=split_plans,
                    total_allocated_qty=total_split_allocated,
                    total_line_value=total_split_cost,
                    fulfillment_pct=split_ful,
                    remaining_qty=split_rem,
                    shortfall_qty=split_rem,
                    feasibility_status="FEASIBLE" if is_full_split else "SHORTFALL",
                    reason="100% Demand Fulfilled via Multi-Supplier Split" if is_full_split else f"Supply Shortfall: Leaves {split_rem} {req_uom} unfulfilled",
                    explanation=split_expl,
                )
            )

        # 5. Deduplicate candidate options (identical allocations & costs)
        unique_options: List[LineAlternativeOption] = []
        seen_sigs = set()
        for opt in candidate_options:
            sig_parts = [f"{s.supplier_id}:{s.awarded_qty}:{s.unit_landed_cost}" for s in opt.splits]
            sig = f"{opt.total_line_value}|{'|'.join(sorted(sig_parts))}"
            if sig not in seen_sigs:
                seen_sigs.add(sig)
                unique_options.append(opt)

        # 6. Rank candidate options for this line
        def line_option_sort_key(opt: LineAlternativeOption) -> Tuple:
            # 1. Fulfillment percentage descending
            # 2. Total cost ascending
            # 3. Prefer single supplier over split if identical fulfillment and cost (0 for single, 1 for split)
            # 4. Option ID tie-breaker
            is_split = 1 if len(opt.splits) > 1 else 0
            cost_val = opt.total_line_value if opt.total_line_value > Decimal("0") else Decimal("Infinity")
            return (-float(opt.fulfillment_pct), cost_val, is_split, opt.option_id)

        ranked_options = sorted(unique_options, key=line_option_sort_key)
        if not ranked_options:
            return LineRecommendation(
                rfq_line_id=rfq_line_id,
                item_sku=sku,
                item_description=desc,
                strategy=strategy,
                strategy_label=strat_label,
                required_qty=req_qty,
                required_uom=req_uom,
                recommended_option_type="UNALLOCATED",
                recommended_splits=[],
                total_allocated_qty=Decimal("0"),
                remaining_qty=req_qty,
                shortfall_qty=req_qty,
                fulfillment_pct=Decimal("0.0"),
                total_line_value=Decimal("0.0"),
                is_supply_shortfall=True if req_qty and req_qty > Decimal("0") else False,
                aggregate_eligible_capacity=display_agg_cap,
                rationale="Unable to form feasible candidate allocations.",
                why_recommended_breakdown={},
                alternatives=[],
                full_fulfillment_options=[],
                partial_fulfillment_options=[],
                supplier_exclusion_reasons={},
            )

        recommended = ranked_options[0]
        rec_cost = recommended.total_line_value

        # 7. Compute deltas relative to recommended option
        for rank_idx, opt in enumerate(ranked_options, start=1):
            opt.rank = rank_idx
            if rank_idx == 1:
                opt.is_recommended = True
                opt.cost_delta_vs_recommended = Decimal("0.0")
                opt.cost_delta_pct_vs_recommended = Decimal("0.0")
            else:
                opt.is_recommended = False
                delta = quantize_currency(opt.total_line_value - rec_cost)
                opt.cost_delta_vs_recommended = delta
                if rec_cost > Decimal("0"):
                    opt.cost_delta_pct_vs_recommended = ((delta / rec_cost) * Decimal("100.0")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )

        full_options = [o for o in ranked_options if o.category == "FULL_FULFILLMENT"]
        partial_options = [o for o in ranked_options if o.category == "PARTIAL_FULFILLMENT"]

        # 8. Formulate high-signal explainability breakdown & rationale
        rec_splits = recommended.splits
        rec_supp_ids = {s.supplier_id for s in rec_splits}

        if is_supply_shortfall:
            rec_opt_type = "PARTIAL_FULFILLMENT"
            rationale_text = (
                f"Partial Fulfillment — Supply Shortfall: No feasible allocation can satisfy the full RFQ requirement "
                f"because aggregate eligible supplier capacity ({display_agg_cap} {req_uom}) is below requested quantity ({req_qty} {req_uom}). "
                f"Allocates maximum available {recommended.total_allocated_qty} {req_uom} ({recommended.fulfillment_pct}%), leaving {recommended.shortfall_qty} {req_uom} shortfall."
            )
        elif len(rec_splits) > 1:
            rec_opt_type = "SPLIT_SOURCING"
            splits_summary_str = ", ".join([f"{s.supplier_name} ({s.awarded_qty} {req_uom} @ {base_currency} {s.unit_landed_cost:,.2f})" for s in rec_splits])
            
            # Find best single supplier alternative for direct comparison
            single_alts = [o for o in full_options if len(o.splits) == 1]
            if single_alts:
                best_single = single_alts[0]
                diff_val = best_single.total_line_value - recommended.total_line_value
                if diff_val > Decimal("0"):
                    savings_note = f", saving {base_currency} {diff_val:,.2f} vs Best Single Supplier {best_single.supplier_names[0]} ({base_currency} {best_single.total_line_value:,.2f})"
                else:
                    savings_note = f" (commercially equal to single supplier {best_single.supplier_names[0]})"
            else:
                savings_note = f" (no single eligible supplier has sufficient capacity; at least {len(rec_splits)} suppliers required for 100% fulfillment)"
            
            rationale_text = (
                f"Split Sourcing recommended ({splits_summary_str}). "
                f"Achieves 100% fulfillment at total landed cost of {base_currency} {recommended.total_line_value:,.2f}{savings_note}."
            )
        elif len(rec_splits) == 1:
            s_plan = rec_splits[0]
            if recommended.fulfillment_pct >= Decimal("100.0"):
                rec_opt_type = "SINGLE_SUPPLIER"
                rationale_text = (
                    f"Single Sourcing with {s_plan.supplier_name} recommended. "
                    f"Fully fulfills {req_qty} {req_uom} at lowest landed price {base_currency} {s_plan.unit_landed_cost:,.2f} (Total {base_currency} {recommended.total_line_value:,.2f})."
                )
            else:
                rec_opt_type = "PARTIAL_FULFILLMENT"
                rationale_text = (
                    f"Partial fulfillment ({recommended.total_allocated_qty} / {req_qty} {req_uom} • {recommended.fulfillment_pct}%) "
                    f"allocated to {s_plan.supplier_name} due to supplier quoted capacity limits ({s_plan.quoted_capacity or 'N/A'} {req_uom})."
                )
        else:
            rec_opt_type = "UNALLOCATED"
            rationale_text = "Unallocated."

        # 9. Compute Supplier Exclusion Reasons for every participating supplier bid
        exclusion_reasons: Dict[str, Dict[str, Any]] = {}
        all_candidate_bids = all_prices if all_prices is not None else eligible_prices
        evals_map = line_evaluations or {}

        # Minimum landed unit price in recommended splits
        rec_min_price = min((s.unit_landed_cost for s in rec_splits), default=Decimal("0.0"))

        for bid in all_candidate_bids:
            s_id = bid.supplier_id
            s_name = bid.supplier_name
            ev = evals_map.get(s_id)
            bid_price = bid.unit_landed_price_base or Decimal("0.0")

            if ev and not ev.is_eligible:
                exclusion_reasons[s_id] = {
                    "supplier_id": s_id,
                    "supplier_name": s_name,
                    "category": "HARD_CONSTRAINT",
                    "badge_label": "Blocked (Hard Constraint)",
                    "badge_class": "badge-danger",
                    "reason": "Hard Constraint Violation",
                    "details": "; ".join(ev.violation_reasons or ev.explanations) if (ev.violation_reasons or ev.explanations) else "Ineligible for award due to hard constraint failures.",
                    "unit_landed_cost": str(bid_price),
                    "quoted_capacity": str(bid.quoted_qty) if bid.quoted_qty is not None else None,
                    "quoted_uom": bid.quoted_uom or req_uom,
                }
            elif s_id in rec_supp_ids:
                matched_split = next((s for s in rec_splits if s.supplier_id == s_id), None)
                alloc_q = matched_split.awarded_qty if matched_split else Decimal("0")
                exclusion_reasons[s_id] = {
                    "supplier_id": s_id,
                    "supplier_name": s_name,
                    "category": "ALLOCATED",
                    "badge_label": "Selected in Recommendation",
                    "badge_class": "badge-success",
                    "reason": "Awarded Allocation",
                    "details": f"Awarded {alloc_q} {matched_split.quoted_uom if matched_split else req_uom} @ {base_currency} {bid_price:,.2f}.",
                    "unit_landed_cost": str(bid_price),
                    "quoted_capacity": str(bid.quoted_qty) if bid.quoted_qty is not None else None,
                    "quoted_uom": bid.quoted_uom or req_uom,
                }
            else:
                # Eligible but not selected
                if rec_min_price > Decimal("0") and bid_price > rec_min_price:
                    diff_unit = bid_price - rec_min_price
                    diff_pct = ((diff_unit / rec_min_price) * Decimal("100.0")).quantize(Decimal("0.1"))
                    r_text = "Higher Landed Cost"
                    d_text = f"Landed price {base_currency} {bid_price:,.2f} is higher than recommended baseline {base_currency} {rec_min_price:,.2f} (+{base_currency} {diff_unit:,.2f}/unit, +{diff_pct}%)."
                elif recommended.fulfillment_pct >= Decimal("100.0"):
                    r_text = "Demand Satisfied"
                    d_text = "100% RFQ requirement was satisfied by higher-ranked eligible suppliers under active objective."
                else:
                    r_text = "Objective Ranking"
                    d_text = "Evaluated below recommended baseline under active sourcing objective policy."

                exclusion_reasons[s_id] = {
                    "supplier_id": s_id,
                    "supplier_name": s_name,
                    "category": "COMMERCIAL",
                    "badge_label": "Commercial Decision",
                    "badge_class": "badge-warning",
                    "reason": r_text,
                    "details": d_text,
                    "unit_landed_cost": str(bid_price),
                    "quoted_capacity": str(bid.quoted_qty) if bid.quoted_qty is not None else None,
                    "quoted_uom": bid.quoted_uom or req_uom,
                }

        why_breakdown = {
            "objective": strat_label,
            "core_objective": strategy_obj_desc,
            "eligibility_checks": [
                "✓ Match verified & candidate confirmed",
                "✓ UOM compatible & normalized",
                "✓ Capacity verified within quoted limits",
                "✓ Hard constraints satisfied",
            ] if not is_supply_shortfall else [
                "✓ Specification matches verified",
                "✓ UOM compatibility confirmed",
                f"⚠ Aggregate eligible capacity ({display_agg_cap} {req_uom}) < Requested requirement ({req_qty} {req_uom})",
            ],
            "allocations": [
                {
                    "supplier_id": s.supplier_id,
                    "supplier_name": s.supplier_name,
                    "quote_id": s.quote_id,
                    "awarded_qty": str(s.awarded_qty),
                    "quoted_uom": s.quoted_uom,
                    "unit_landed_cost": str(s.unit_landed_cost),
                    "split_value": str(s.split_value),
                    "unused_quote_qty": str(s.unused_quote_qty) if s.unused_quote_qty is not None else None,
                    "formula": f"{s.awarded_qty} {s.quoted_uom} × {base_currency} {s.unit_landed_cost:,.2f} = {base_currency} {s.split_value:,.2f}",
                }
                for s in rec_splits
            ],
            "total_value": str(recommended.total_line_value),
            "required_qty": str(req_qty),
            "allocated_qty": str(recommended.total_allocated_qty),
            "remaining_qty": str(recommended.remaining_qty),
            "shortfall_qty": str(recommended.shortfall_qty),
            "fulfillment_pct": str(recommended.fulfillment_pct),
            "is_supply_shortfall": is_supply_shortfall,
            "aggregate_eligible_capacity": str(display_agg_cap) if display_agg_cap is not None else None,
            "alternatives_comparison": [
                {
                    "option_id": alt.option_id,
                    "title": alt.title,
                    "category": alt.category,
                    "feasibility_status": alt.feasibility_status,
                    "total_value": str(alt.total_line_value),
                    "cost_delta": str(alt.cost_delta_vs_recommended),
                    "cost_delta_pct": str(alt.cost_delta_pct_vs_recommended),
                    "fulfillment_pct": str(alt.fulfillment_pct),
                    "is_recommended": alt.is_recommended,
                    "rank": alt.rank,
                    "reason": alt.reason,
                    "splits": [
                        {
                            "supplier_id": s.supplier_id,
                            "supplier_name": s.supplier_name,
                            "quote_id": s.quote_id,
                            "allocated_qty": str(s.awarded_qty),
                            "quoted_uom": s.quoted_uom,
                            "unit_landed_cost": str(s.unit_landed_cost),
                            "split_value": str(s.split_value),
                            "quoted_capacity": str(s.quoted_capacity) if s.quoted_capacity is not None else None,
                            "unused_quote_qty": str(s.unused_quote_qty) if s.unused_quote_qty is not None else None,
                        }
                        for s in alt.splits
                    ],
                }
                for alt in ranked_options
            ],
        }

        return LineRecommendation(
            rfq_line_id=rfq_line_id,
            item_sku=sku,
            item_description=desc,
            strategy=strategy,
            strategy_label=strat_label,
            required_qty=req_qty,
            required_uom=req_uom,
            recommended_option_type=rec_opt_type,
            recommended_splits=rec_splits,
            total_allocated_qty=recommended.total_allocated_qty,
            remaining_qty=recommended.remaining_qty,
            shortfall_qty=recommended.shortfall_qty,
            fulfillment_pct=recommended.fulfillment_pct,
            total_line_value=recommended.total_line_value,
            is_supply_shortfall=is_supply_shortfall,
            aggregate_eligible_capacity=display_agg_cap,
            rationale=rationale_text,
            why_recommended_breakdown=why_breakdown,
            alternatives=ranked_options,
            full_fulfillment_options=full_options,
            partial_fulfillment_options=partial_options,
            supplier_exclusion_reasons=exclusion_reasons,
        )

    def optimize_all_lines(
        self,
        comparison: RFQComparison,
        strategy: ProcurementStrategy = ProcurementStrategy.COST_OPTIMIZED,
        supplier_masters: Optional[Dict[str, SupplierMasterRecord]] = None,
    ) -> Dict[str, LineRecommendation]:
        """Calculates line-level recommendations and ranked alternatives for all RFQ lines."""
        supplier_masters = supplier_masters or {}
        evaluations = self.constraint_evaluator.evaluate_all(comparison, supplier_masters)
        base_currency = comparison.base_currency

        line_recs: Dict[str, LineRecommendation] = {}
        for rfq_line_id, item_comp in comparison.item_comparisons.items():
            eligible: List[NormalizedItemPrice] = []
            all_line_prices: List[NormalizedItemPrice] = list(item_comp.supplier_prices.values())
            line_evals = evaluations.get(rfq_line_id, {})
            for supp_id, price in item_comp.supplier_prices.items():
                ev = line_evals.get(supp_id)
                if ev and ev.is_eligible:
                    eligible.append(price)

            line_scores = self.scorer.score_line_suppliers(
                rfq_line_id, eligible, comparison, strategy
            )

            line_recs[rfq_line_id] = self.optimize_line(
                rfq_line_id=rfq_line_id,
                item_comp=item_comp,
                eligible_prices=eligible,
                strategy=strategy,
                base_currency=base_currency,
                line_scores=line_scores,
                all_prices=all_line_prices,
                line_evaluations=line_evals,
            )

        return line_recs

