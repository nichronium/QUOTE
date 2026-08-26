"""
Evidence-Driven Multi-Signal Sourcing Scorer.
Computes objective scores for eligible suppliers without data fabrication.
INVARIANT: Missing metrics are tracked explicitly as None and never assigned fake default scores.
"""

from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from comparison.models import NormalizedItemPrice, RFQComparison
from sourcing.models import (
    ProcurementStrategy,
    StrategyWeights,
    SupplierEvidence,
)
from sourcing.strategy import get_strategy_weights


class SourcingScorer:
    """Computes multi-dimensional objective scores based strictly on authoritative evidence."""

    def extract_supplier_evidence(
        self,
        rfq_line_id: str,
        price: NormalizedItemPrice,
        comparison: RFQComparison,
    ) -> SupplierEvidence:
        """Extracts available evidence from comparison models for an item-supplier pair."""
        supp_summary = comparison.suppliers.get(price.supplier_id)
        terms = supp_summary.commercial_terms if supp_summary else None

        lead_time = price.lead_time_days
        if lead_time is None and terms:
            lead_time = terms.default_lead_time_days

        spec_match = None
        if price.matched_quote_item and hasattr(price.matched_quote_item, "candidate_item"):
            cand = price.matched_quote_item.candidate_item
            if cand and hasattr(cand, "match_score"):
                spec_match = float(cand.match_score)

        credit_days = terms.credit_days if terms else None
        advance_pct = terms.advance_pct if terms else None
        payment_terms_raw = terms.payment_terms_raw if terms else None

        return SupplierEvidence(
            supplier_id=price.supplier_id,
            supplier_name=price.supplier_name,
            rfq_line_id=rfq_line_id,
            unit_landed_price_base=price.unit_landed_price_base,
            quoted_capacity=price.quoted_qty,
            quoted_uom=price.quoted_uom,
            lead_time_days=lead_time,
            moq=price.moq,
            spec_match_score=spec_match,
            quality_rating=None,  # Not present in base extraction schema, explicitly None
            payment_terms_raw=payment_terms_raw,
            credit_days=credit_days,
            advance_pct=advance_pct,
            has_lead_time=(lead_time is not None),
            has_quality_rating=False,
            has_spec_match=(spec_match is not None),
            has_payment_terms=(credit_days is not None or advance_pct is not None or payment_terms_raw is not None),
        )

    def score_line_suppliers(
        self,
        rfq_line_id: str,
        eligible_prices: List[NormalizedItemPrice],
        comparison: RFQComparison,
        strategy: ProcurementStrategy,
    ) -> Dict[str, Tuple[float, Dict[str, Optional[float]], SupplierEvidence]]:
        """
        Scores all eligible suppliers for a specific RFQ Line under the selected strategy.
        Returns: supplier_id -> (composite_score, sub_scores_dict, evidence)
        """
        if not eligible_prices:
            return {}

        weights = get_strategy_weights(strategy)
        results: Dict[str, Tuple[float, Dict[str, Optional[float]], SupplierEvidence]] = {}

        # 1. Price envelope for normalization
        valid_prices = [
            float(p.unit_landed_price_base)
            for p in eligible_prices
            if p.unit_landed_price_base is not None and p.unit_landed_price_base > Decimal("0")
        ]
        min_price = min(valid_prices) if valid_prices else 1.0
        max_price = max(valid_prices) if valid_prices else 1.0
        price_span = max(max_price - min_price, 0.0001)

        # 2. Lead time envelope
        lead_times = []
        for p in eligible_prices:
            lt = p.lead_time_days
            if lt is None and p.supplier_id in comparison.suppliers:
                terms = comparison.suppliers[p.supplier_id].commercial_terms
                lt = terms.default_lead_time_days if terms else None
            if lt is not None:
                lead_times.append(lt)
        min_lt = min(lead_times) if lead_times else None
        max_lt = max(lead_times) if lead_times else None
        lt_span = (max_lt - min_lt) if (min_lt is not None and max_lt is not None and max_lt > min_lt) else 1.0

        for price in eligible_prices:
            evidence = self.extract_supplier_evidence(rfq_line_id, price, comparison)
            
            # --- Cost Score (0 - 100, higher is better) ---
            if price.unit_landed_price_base is not None and price.unit_landed_price_base > Decimal("0"):
                p_val = float(price.unit_landed_price_base)
                if min_price == max_price:
                    cost_score = 100.0
                else:
                    # Lowest price gets 100, highest gets proportional score down to 50
                    cost_score = max(50.0, 100.0 - ((p_val - min_price) / price_span) * 50.0)
            else:
                cost_score = 0.0

            # --- Delivery Score (0 - 100, if available) ---
            delivery_score: Optional[float] = None
            if evidence.lead_time_days is not None:
                if min_lt == max_lt:
                    delivery_score = 100.0
                else:
                    delivery_score = max(50.0, 100.0 - ((evidence.lead_time_days - min_lt) / lt_span) * 50.0)

            # --- Quality / Spec Match Score (0 - 100, if available) ---
            quality_score: Optional[float] = None
            if evidence.spec_match_score is not None:
                quality_score = max(0.0, min(100.0, evidence.spec_match_score * 100.0))

            # --- Risk / Commercial Terms Score (0 - 100, if available) ---
            risk_score: Optional[float] = None
            if evidence.credit_days is not None or evidence.advance_pct is not None:
                r_val = 75.0
                if evidence.credit_days and evidence.credit_days >= 30:
                    r_val += 25.0
                elif evidence.advance_pct and evidence.advance_pct > Decimal("0"):
                    r_val -= min(35.0, float(evidence.advance_pct))
                risk_score = max(0.0, min(100.0, r_val))

            # --- Composite Score Calculation (Dynamic Weight Renormalization) ---
            active_weights_sum = weights.cost_weight
            weighted_score_sum = cost_score * weights.cost_weight

            if delivery_score is not None:
                active_weights_sum += weights.delivery_weight
                weighted_score_sum += delivery_score * weights.delivery_weight

            if quality_score is not None:
                active_weights_sum += weights.quality_weight
                weighted_score_sum += quality_score * weights.quality_weight

            if risk_score is not None:
                active_weights_sum += weights.risk_weight
                weighted_score_sum += risk_score * weights.risk_weight

            composite = weighted_score_sum / active_weights_sum if active_weights_sum > 0 else cost_score

            sub_scores = {
                "cost_score": round(cost_score, 1),
                "delivery_score": round(delivery_score, 1) if delivery_score is not None else None,
                "quality_score": round(quality_score, 1) if quality_score is not None else None,
                "risk_score": round(risk_score, 1) if risk_score is not None else None,
            }

            results[price.supplier_id] = (round(composite, 2), sub_scores, evidence)

        return results
