"""
Ranking and Recommendation Engine.
Computes deterministic, explainable L1/L2/L3 quote-level rankings and item-level split sourcing recommendations.
INVARIANT:
- Only fully comparable suppliers enter automatic quote-level L1 ranking.
- Pure landed cost is separated from commercial advantages/disadvantages.
- Transparent human-readable explanations accompany every ranking.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from core.canonical_quote import quantize_currency
from comparison.models import (
    ComparisonIssue,
    ItemComparison,
    NormalizedItemPrice,
    RFQComparison,
    SupplierComparison,
)


class ItemSplitRecommendation(BaseModel):
    """Independent item-level L1 / L2 supplier recommendation."""
    rfq_line_id: str
    item_description: str
    requested_quantity: Decimal
    requested_uom: str
    base_currency: str

    l1_supplier_id: Optional[str] = None
    l1_supplier_name: Optional[str] = None
    l1_unit_landed_price_base: Optional[Decimal] = None
    l1_line_total_base: Optional[Decimal] = None
    l1_lead_time_days: Optional[int] = None

    l2_supplier_id: Optional[str] = None
    l2_supplier_name: Optional[str] = None
    l2_unit_landed_price_base: Optional[Decimal] = None
    l2_line_total_base: Optional[Decimal] = None

    price_gap_l1_to_l2: Optional[Decimal] = None
    price_gap_pct_l1_to_l2: Optional[Decimal] = None

    is_valid: bool = False
    explanation: str = ""


class SupplierRankingResult(BaseModel):
    """Quote-level supplier ranking entry with full commercial audit trail."""
    rank: int  # 1 for L1, 2 for L2, etc.
    supplier_id: str
    supplier_name: str
    total_landed_cost_base: Optional[Decimal] = None
    price_delta_to_l1: Decimal = Decimal("0.0")
    price_delta_pct_to_l1: Decimal = Decimal("0.0")

    is_eligible: bool = True
    ineligibility_reasons: List[str] = Field(default_factory=list)

    advantages: List[str] = Field(default_factory=list)
    disadvantages: List[str] = Field(default_factory=list)
    explanation: str = ""


class GlobalRankingReport(BaseModel):
    """Complete enterprise ranking report."""
    rfq_id: str
    base_currency: str

    l1_supplier: Optional[SupplierRankingResult] = None
    l2_supplier: Optional[SupplierRankingResult] = None
    l3_supplier: Optional[SupplierRankingResult] = None

    ranked_suppliers: List[SupplierRankingResult] = Field(default_factory=list)
    ineligible_suppliers: List[SupplierRankingResult] = Field(default_factory=list)

    item_split_recommendations: Dict[str, ItemSplitRecommendation] = Field(default_factory=dict)
    total_split_sourcing_cost_base: Optional[Decimal] = None
    total_split_savings_vs_l1: Decimal = Decimal("0.0")


class RankingEngine:
    """Enterprise decision layer computing explainable rankings and item splits."""

    def generate_ranking_report(self, comparison: RFQComparison) -> GlobalRankingReport:
        """Computes quote-level rankings and item-level split recommendations across complete and partial suppliers."""
        base_currency = comparison.base_currency

        # 1. Compute Item-level Split Recommendations (includes all suppliers with valid line prices)
        item_splits = self._compute_item_split_recommendations(comparison)

        # 2. Separate Complete vs Partial vs Ineligible Suppliers
        eligible_suppliers: List[SupplierComparison] = []
        ineligible_results: List[SupplierRankingResult] = []

        for supp_id, supp in comparison.suppliers.items():
            ineligibility_reasons = []

            # Check if partial vs complete
            is_partial = (not supp.is_fully_comparable and supp.comparable_items_count > 0)

            if supp.comparable_items_count == 0:
                ineligibility_reasons.append("Zero comparable items matched for this supplier")

            if supp.total_quote_landed_base is None and not is_partial:
                ineligibility_reasons.append("Missing valid base-currency landed cost calculation")

            if supp.exchange_rate is None and supp.source_currency != base_currency:
                ineligibility_reasons.append(f"Missing exchange rate for currency '{supp.source_currency}'")

            for issue in supp.issues:
                if issue.severity.value in ("CRITICAL", "BLOCKING") and not is_partial:
                    ineligibility_reasons.append(f"Blocking issue: {issue.message}")

            if is_partial:
                # Partial suppliers participate in line-item split awards but are documented separately for whole-RFQ award
                ineligible_results.append(SupplierRankingResult(
                    rank=999,
                    supplier_id=supp_id,
                    supplier_name=supp.supplier_name,
                    total_landed_cost_base=supp.total_quote_landed_base,
                    is_eligible=False,
                    ineligibility_reasons=[f"Partial scope coverage ({supp.comparable_items_count} items quoted). Eligible for individual line-item awards."],
                    explanation=f"Partial Scope ({supp.comparable_items_count} items comparable) — Eligible for line-item split awards"
                ))
            elif ineligibility_reasons:
                ineligible_results.append(SupplierRankingResult(
                    rank=999,
                    supplier_id=supp_id,
                    supplier_name=supp.supplier_name,
                    total_landed_cost_base=supp.total_quote_landed_base,
                    is_eligible=False,
                    ineligibility_reasons=ineligibility_reasons,
                    explanation=f"Ineligible for whole-RFQ single award: {'; '.join(ineligibility_reasons)}"
                ))
            else:
                eligible_suppliers.append(supp)

        # 3. Deterministic Sorting of Eligible Suppliers (lowest landed cost first, tie-break on supplier_id)
        eligible_suppliers.sort(
            key=lambda s: (s.total_quote_landed_base if s.total_quote_landed_base is not None else Decimal("Infinity"), s.supplier_id)
        )

        ranked_results: List[SupplierRankingResult] = []
        l1_cost: Optional[Decimal] = eligible_suppliers[0].total_quote_landed_base if eligible_suppliers else None

        for idx, supp in enumerate(eligible_suppliers, start=1):
            cost = supp.total_quote_landed_base or Decimal("0.0")
            delta = quantize_currency(cost - l1_cost) if l1_cost is not None else Decimal("0.0")
            delta_pct = Decimal("0.0")
            if l1_cost and l1_cost > Decimal("0.0"):
                delta_pct = ((delta / l1_cost) * Decimal("100.0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            advantages: List[str] = []
            disadvantages: List[str] = []

            if idx == 1:
                advantages.append(f"Lowest overall landed cost ({base_currency} {cost:,.2f})")
            else:
                disadvantages.append(f"{base_currency} {delta:,.2f} (+{delta_pct}%) higher landed cost than L1")

            # Commercial terms evaluation
            terms = supp.commercial_terms
            if terms.credit_days and terms.credit_days >= 30:
                advantages.append(f"Favorable credit terms ({terms.credit_days} days)")
            elif terms.advance_pct and terms.advance_pct > Decimal("0.0"):
                disadvantages.append(f"Advance payment required ({terms.advance_pct}%)")

            if terms.default_lead_time_days is not None:
                if terms.default_lead_time_days <= 7:
                    advantages.append(f"Fast delivery lead time ({terms.default_lead_time_days} days)")
                elif terms.default_lead_time_days > 30:
                    disadvantages.append(f"Long delivery lead time ({terms.default_lead_time_days} days)")

            expl = f"Rank L{idx}: {supp.supplier_name} — Landed Total {base_currency} {cost:,.2f}"
            if idx > 1:
                expl += f" (+{base_currency} {delta:,.2f} vs L1)"

            ranked_results.append(SupplierRankingResult(
                rank=idx,
                supplier_id=supp.supplier_id,
                supplier_name=supp.supplier_name,
                total_landed_cost_base=cost,
                price_delta_to_l1=delta,
                price_delta_pct_to_l1=delta_pct,
                is_eligible=True,
                advantages=advantages,
                disadvantages=disadvantages,
                explanation=expl
            ))

        # 4. Calculate Split Sourcing Savings vs Single-Vendor L1
        total_split_cost = Decimal("0.0")
        all_items_have_l1 = True
        for rec in item_splits.values():
            if rec.is_valid and rec.l1_line_total_base is not None:
                total_split_cost += rec.l1_line_total_base
            else:
                all_items_have_l1 = False

        split_savings = Decimal("0.0")
        if all_items_have_l1 and l1_cost is not None and l1_cost > total_split_cost:
            split_savings = quantize_currency(l1_cost - total_split_cost)

        return GlobalRankingReport(
            rfq_id=comparison.rfq_id,
            base_currency=base_currency,
            l1_supplier=ranked_results[0] if len(ranked_results) >= 1 else None,
            l2_supplier=ranked_results[1] if len(ranked_results) >= 2 else None,
            l3_supplier=ranked_results[2] if len(ranked_results) >= 3 else None,
            ranked_suppliers=ranked_results,
            ineligible_suppliers=ineligible_results,
            item_split_recommendations=item_splits,
            total_split_sourcing_cost_base=total_split_cost if all_items_have_l1 else None,
            total_split_savings_vs_l1=split_savings
        )

    def _compute_item_split_recommendations(
        self, comparison: RFQComparison
    ) -> Dict[str, ItemSplitRecommendation]:
        """Computes independent item-level L1 / L2 recommendations."""
        recommendations: Dict[str, ItemSplitRecommendation] = {}
        base_curr = comparison.base_currency

        for rfq_line_id, item_comp in comparison.item_comparisons.items():
            valid_prices: List[NormalizedItemPrice] = [
                p for p in item_comp.supplier_prices.values()
                if p.is_comparable and p.unit_landed_price_base is not None
            ]

            # Deterministic sort by unit landed price in base currency
            valid_prices.sort(
                key=lambda p: (p.unit_landed_price_base if p.unit_landed_price_base is not None else Decimal("Infinity"), p.supplier_id)
            )

            if valid_prices:
                l1 = valid_prices[0]
                l2 = valid_prices[1] if len(valid_prices) > 1 else None

                price_gap = None
                price_gap_pct = None
                if l2 and l2.unit_landed_price_base is not None and l1.unit_landed_price_base is not None:
                    price_gap = quantize_currency(l2.unit_landed_price_base - l1.unit_landed_price_base)
                    if l2.unit_landed_price_base > Decimal("0.0"):
                        price_gap_pct = ((price_gap / l2.unit_landed_price_base) * Decimal("100.0")).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )

                expl = f"L1 for {item_comp.item_description}: {l1.supplier_name} at {base_curr} {l1.unit_landed_price_base:,.2f}/{item_comp.requested_uom}"
                if l2:
                    expl += f" (L2: {l2.supplier_name} at {base_curr} {l2.unit_landed_price_base:,.2f}, gap: {price_gap_pct}%)"

                recommendations[rfq_line_id] = ItemSplitRecommendation(
                    rfq_line_id=rfq_line_id,
                    item_description=item_comp.item_description,
                    requested_quantity=item_comp.requested_quantity,
                    requested_uom=item_comp.requested_uom,
                    base_currency=base_curr,
                    l1_supplier_id=l1.supplier_id,
                    l1_supplier_name=l1.supplier_name,
                    l1_unit_landed_price_base=l1.unit_landed_price_base,
                    l1_line_total_base=l1.line_total_landed_base,
                    l1_lead_time_days=l1.lead_time_days,
                    l2_supplier_id=l2.supplier_id if l2 else None,
                    l2_supplier_name=l2.supplier_name if l2 else None,
                    l2_unit_landed_price_base=l2.unit_landed_price_base if l2 else None,
                    l2_line_total_base=l2.line_total_landed_base if l2 else None,
                    price_gap_l1_to_l2=price_gap,
                    price_gap_pct_l1_to_l2=price_gap_pct,
                    is_valid=True,
                    explanation=expl
                )
            else:
                recommendations[rfq_line_id] = ItemSplitRecommendation(
                    rfq_line_id=rfq_line_id,
                    item_description=item_comp.item_description,
                    requested_quantity=item_comp.requested_quantity,
                    requested_uom=item_comp.requested_uom,
                    base_currency=base_curr,
                    is_valid=False,
                    explanation=f"No comparable supplier prices available for '{item_comp.item_description}'"
                )

        return recommendations
