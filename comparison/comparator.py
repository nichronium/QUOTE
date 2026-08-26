"""
Quotation Comparison Engine.
Produces structured, auditable, multi-supplier comparison matrices against an RFQ.
INVARIANT: Preserves all source provenance, calculations, and comparison issues without mutating source quotes.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from core.canonical_quote import quantize_currency
from matching.models import MatchedQuoteItem, MatchStatus, RFQLineItem
from comparison.models import (
    ChargeAllocationMethod,
    ComparisonIssue,
    ComparisonIssueCode,
    ComparisonIssueSeverity,
    ItemComparison,
    NormalizedItemPrice,
    RFQComparison,
    RFQDocument,
    SupplierComparison,
    SupplierQuoteSubmission,
)
from comparison.normalizer import CommercialNormalizer


class QuotationComparator:
    """Enterprise multi-supplier comparison matrix constructor."""

    def __init__(self):
        self.normalizer = CommercialNormalizer()

    def compare_rfq(
        self,
        rfq: RFQDocument,
        submissions: List[SupplierQuoteSubmission],
        exchange_rates: Optional[Dict[str, Decimal]] = None,
        charge_allocation_method: ChargeAllocationMethod = ChargeAllocationMethod.NONE,
    ) -> RFQComparison:
        """
        Executes complete multi-supplier comparison against an RFQ.
        """
        rates = exchange_rates.copy() if exchange_rates else {}
        base_currency = rfq.base_currency.upper().strip()
        global_issues: List[ComparisonIssue] = []

        item_comparisons: Dict[str, ItemComparison] = {}
        supplier_comparisons: Dict[str, SupplierComparison] = {}

        # 1. Initialize Item Comparisons for each RFQ line item
        for rfq_line in rfq.items:
            item_comparisons[rfq_line.rfq_line_id] = ItemComparison(
                rfq_line_id=rfq_line.rfq_line_id,
                item_description=rfq_line.description,
                requested_quantity=rfq_line.requested_quantity,
                requested_uom=rfq_line.requested_uom,
                supplier_prices={},
                comparable_supplier_ids=[],
                provisional_supplier_ids=[],
                issues=[]
            )

        # 2. Process each supplier quote submission
        for sub in submissions:
            supplier_id = sub.supplier_id
            supplier_prices_for_sub: List[NormalizedItemPrice] = []

            # Determine item-level allocated charges for this supplier quote
            allocated_charges_map = self._compute_allocated_charges(
                sub, rfq, charge_allocation_method
            )

            # Match RFQ items to supplier matched quote items
            for rfq_line in rfq.items:
                matched_item = self._find_matched_item_for_rfq_line(rfq_line, sub.matched_items)

                if matched_item:
                    alloc_charge = allocated_charges_map.get(rfq_line.rfq_line_id, Decimal("0.0"))

                    norm_price = self.normalizer.normalize_item_price(
                        rfq_line=rfq_line,
                        matched_item=matched_item,
                        submission=sub,
                        base_currency=base_currency,
                        exchange_rates=rates,
                        charge_allocation_method=charge_allocation_method,
                        allocated_charge_quoted=alloc_charge
                    )
                    supplier_prices_for_sub.append(norm_price)

                    # Register in item comparison
                    item_comp = item_comparisons[rfq_line.rfq_line_id]
                    item_comp.supplier_prices[supplier_id] = norm_price

                    if norm_price.is_comparable:
                        item_comp.comparable_supplier_ids.append(supplier_id)
                    elif norm_price.is_provisional:
                        item_comp.provisional_supplier_ids.append(supplier_id)

                    for issue in norm_price.issues:
                        item_comp.issues.append(issue)
                else:
                    # Supplier did not quote this RFQ item
                    issue = ComparisonIssue(
                        code=ComparisonIssueCode.INCOMPLETE_QUOTE,
                        severity=ComparisonIssueSeverity.WARNING,
                        message=f"Supplier did not quote RFQ line item '{rfq_line.rfq_line_id}' ({rfq_line.description})",
                        supplier_id=supplier_id,
                        rfq_line_id=rfq_line.rfq_line_id
                    )
                    item_comparisons[rfq_line.rfq_line_id].issues.append(issue)

            # Build supplier-level comparison summary
            supp_comp = self.normalizer.summarize_supplier_comparison(
                submission=sub,
                normalized_prices=supplier_prices_for_sub,
                rfq_items_count=len(rfq.items),
                base_currency=base_currency,
                exchange_rates=rates
            )
            supplier_comparisons[supplier_id] = supp_comp

        return RFQComparison(
            rfq_id=rfq.rfq_id,
            base_currency=base_currency,
            charge_allocation_method=charge_allocation_method,
            exchange_rates_used=rates,
            suppliers=supplier_comparisons,
            item_comparisons=item_comparisons,
            global_issues=global_issues
        )

    def _compute_allocated_charges(
        self,
        submission: SupplierQuoteSubmission,
        rfq: RFQDocument,
        method: ChargeAllocationMethod
    ) -> Dict[str, Decimal]:
        """Calculates allocated commercial charges per RFQ line item in quoted currency."""
        allocated_map: Dict[str, Decimal] = {}
        if method == ChargeAllocationMethod.NONE:
            return allocated_map

        total_charges = sum(
            (chg.total_with_tax for chg in submission.canonical_quote.additional_charges),
            Decimal("0.0")
        )
        if total_charges <= Decimal("0.0"):
            return allocated_map

        if method == ChargeAllocationMethod.PROPORTIONAL_QUANTITY:
            total_qty = sum((item.requested_quantity for item in rfq.items), Decimal("0.0"))
            if total_qty > Decimal("0.0"):
                for item in rfq.items:
                    share = (item.requested_quantity / total_qty) * total_charges
                    allocated_map[item.rfq_line_id] = quantize_currency(share)

        elif method == ChargeAllocationMethod.PROPORTIONAL_LINE_VALUE:
            # Estimate tentative taxable line values
            line_values: Dict[str, Decimal] = {}
            total_val = Decimal("0.0")
            for rfq_line in rfq.items:
                m_item = self._find_matched_item_for_rfq_line(rfq_line, submission.matched_items)
                if m_item and m_item.quote_item.unit_price is not None and rfq_line.requested_quantity is not None:
                    val = quantize_currency(m_item.quote_item.unit_price * rfq_line.requested_quantity)
                    line_values[rfq_line.rfq_line_id] = val
                    total_val += val

            if total_val > Decimal("0.0"):
                for rfq_line_id, val in line_values.items():
                    share = (val / total_val) * total_charges
                    allocated_map[rfq_line_id] = quantize_currency(share)

        return allocated_map

    def _find_matched_item_for_rfq_line(
        self, rfq_line: RFQLineItem, matched_items: List[MatchedQuoteItem]
    ) -> Optional[MatchedQuoteItem]:
        """Matches RFQ line to supplier matched quote item."""
        for m in matched_items:
            # Check RFQ Match ID
            if m.rfq_match and m.rfq_match.candidate_item_id == rfq_line.rfq_line_id:
                return m
            # Check Item Master ID
            if m.item_master_match and rfq_line.internal_item_id and m.item_master_match.candidate_item_id == rfq_line.internal_item_id:
                return m
            # Check SKU
            if m.item_master_match and rfq_line.sku and m.item_master_match.candidate_sku == rfq_line.sku:
                return m
        return None
