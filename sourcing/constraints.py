"""
Hard Constraint Evaluator for Sourcing Optimization.
Ensures only fully compliant, dimension-safe, active suppliers enter feasible optimization scenarios.
INVARIANT: Hard constraint violations strictly mark suppliers INELIGIBLE (never just lower score).
"""

from decimal import Decimal
from typing import Dict, List, Optional, Set

from comparison.models import (
    ComparisonIssueCode,
    ComparisonIssueSeverity,
    ItemComparison,
    NormalizedItemPrice,
    RFQComparison,
    SupplierComparison,
)
from matching.models import DecisionBand, SupplierMasterRecord
from sourcing.models import (
    HardConstraintCode,
    SupplierConstraintEvaluation,
)


class ConstraintEvaluator:
    """Evaluates hard procurement constraints across suppliers and RFQ lines."""

    def evaluate_line_supplier(
        self,
        rfq_line_id: str,
        price: NormalizedItemPrice,
        supplier_master: Optional[SupplierMasterRecord] = None,
        rfq_price_ceiling: Optional[Decimal] = None,
        max_lead_time_days: Optional[int] = None,
    ) -> SupplierConstraintEvaluation:
        """Evaluates all hard constraints for a specific supplier on an RFQ line."""
        violations: List[HardConstraintCode] = []
        violation_reasons: List[str] = []
        explanations: List[str] = []

        # 1. Check Supplier Master Active Status
        if supplier_master and supplier_master.status.upper() != "ACTIVE":
            violations.append(HardConstraintCode.SUPPLIER_INACTIVE)
            violation_reasons.append(f"Supplier '{supplier_master.supplier_name}' is INACTIVE in Supplier Master")

        # 2. Check Match Candidate Hard Conflict
        matched_item = price.matched_quote_item
        if matched_item and hasattr(matched_item, "candidate_item") and matched_item.candidate_item:
            cand = matched_item.candidate_item
            if getattr(cand, "has_hard_conflict", False) or getattr(cand, "decision_band", None) == DecisionBand.BLOCKED_CONFLICT.value:
                violations.append(HardConstraintCode.BLOCKED_CONFLICT)
                conflict_details = "; ".join(getattr(cand, "conflict_reasons", [])) or "Hard specification mismatch"
                violation_reasons.append(f"Blocked match conflict: {conflict_details}")

        # 3. Check UOM Compatibility
        for issue in price.issues:
            if issue.code == ComparisonIssueCode.UOM_INCOMPATIBLE or issue.severity == ComparisonIssueSeverity.BLOCKING:
                if HardConstraintCode.UOM_INCOMPATIBLE not in violations:
                    violations.append(HardConstraintCode.UOM_INCOMPATIBLE)
                    violation_reasons.append(f"UOM Incompatible: {issue.message}")

        if not price.is_comparable:
            if HardConstraintCode.UOM_INCOMPATIBLE not in violations:
                violations.append(HardConstraintCode.MISSING_REQUIRED_PRICE)
                violation_reasons.append("Item quotation is marked not commercially comparable")

        # 4. Check Base Landed Price Validity
        if price.unit_landed_price_base is None or price.unit_landed_price_base <= Decimal("0.0"):
            if HardConstraintCode.MISSING_REQUIRED_PRICE not in violations:
                violations.append(HardConstraintCode.MISSING_REQUIRED_PRICE)
                violation_reasons.append("Missing valid normalized unit landed price in base currency")

        # 5. Check Quoted Capacity Availability
        if price.quoted_qty is not None and price.quoted_qty <= Decimal("0.0"):
            violations.append(HardConstraintCode.ZERO_CAPACITY)
            violation_reasons.append("Supplier quoted quantity is zero or negative")

        # 6. Check Optional Price Ceiling
        if rfq_price_ceiling is not None and price.unit_landed_price_base is not None:
            if price.unit_landed_price_base > rfq_price_ceiling:
                violations.append(HardConstraintCode.PRICE_CEILING_EXCEEDED)
                violation_reasons.append(
                    f"Unit landed price ({price.unit_landed_price_base}) exceeds buyer ceiling ({rfq_price_ceiling})"
                )

        # 7. Check Optional Lead Time Constraint
        if max_lead_time_days is not None and price.lead_time_days is not None:
            if price.lead_time_days > max_lead_time_days:
                violations.append(HardConstraintCode.LEAD_TIME_EXCEEDED)
                violation_reasons.append(
                    f"Quoted lead time ({price.lead_time_days} days) exceeds maximum allowable ({max_lead_time_days} days)"
                )

        is_eligible = (len(violations) == 0)
        if is_eligible:
            explanations.append("All hard procurement constraints satisfied (specification, UOM, active vendor, capacity).")
        else:
            explanations.extend(violation_reasons)

        return SupplierConstraintEvaluation(
            supplier_id=price.supplier_id,
            supplier_name=price.supplier_name,
            rfq_line_id=rfq_line_id,
            is_eligible=is_eligible,
            violations=violations,
            violation_reasons=violation_reasons,
            explanations=explanations,
        )

    def evaluate_all(
        self,
        comparison: RFQComparison,
        supplier_masters: Optional[Dict[str, SupplierMasterRecord]] = None,
    ) -> Dict[str, Dict[str, SupplierConstraintEvaluation]]:
        """Evaluates all suppliers across all lines in an RFQ comparison."""
        supplier_masters = supplier_masters or {}
        results: Dict[str, Dict[str, SupplierConstraintEvaluation]] = {}

        for rfq_line_id, item_comp in comparison.item_comparisons.items():
            results[rfq_line_id] = {}
            for supp_id, price in item_comp.supplier_prices.items():
                master = supplier_masters.get(supp_id)
                evaluation = self.evaluate_line_supplier(
                    rfq_line_id=rfq_line_id,
                    price=price,
                    supplier_master=master,
                )
                results[rfq_line_id][supp_id] = evaluation

        return results
