"""
Commercial Normalization Engine.
Performs deterministic, dimension-safe, currency-aware price calculations across quotes and RFQ lines.
All calculations use pure Decimal arithmetic with explicit currency quantization.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Tuple

from core.canonical_quote import (
    CanonicalQuote,
    PriceTier,
    QuoteItem,
    quantize_currency,
)
from matching.models import MatchedQuoteItem, MatchStatus, RFQLineItem
from matching.uom_resolver import UOMResolver
from core.currency import CurrencyRateService
from comparison.models import (
    ChargeAllocationMethod,
    ComparisonIssue,
    ComparisonIssueCode,
    ComparisonIssueSeverity,
    NormalizedItemPrice,
    SupplierCommercialTerms,
    SupplierComparison,
    SupplierQuoteSubmission,
)


class CommercialNormalizer:
    """Normalizes supplier items, volume tiers, taxes, commercial charges, and currencies."""

    def __init__(self):
        self.uom_resolver = UOMResolver()

    def normalize_item_price(
        self,
        rfq_line: RFQLineItem,
        matched_item: MatchedQuoteItem,
        submission: SupplierQuoteSubmission,
        base_currency: str,
        exchange_rates: Dict[str, Decimal],
        charge_allocation_method: ChargeAllocationMethod = ChargeAllocationMethod.NONE,
        allocated_charge_quoted: Decimal = Decimal("0.0"),
    ) -> NormalizedItemPrice:
        """
        Calculates normalized price for an RFQ item from a matched quote item.
        Preserves original QuoteItem without mutation.
        """
        quote_item = matched_item.quote_item
        issues: List[ComparisonIssue] = []

        supplier_id = submission.supplier_id
        supplier_name = submission.supplier_name
        quoted_currency = submission.canonical_quote.currency.upper().strip()
        base_curr = base_currency.upper().strip()

        # 1. UOM Compatibility & Conversion
        if rfq_line.requested_uom and quote_item.quoted_uom:
            uom_result = self.uom_resolver.resolve_uom_conversion(
                source_uom=quote_item.quoted_uom,
                target_uom=rfq_line.requested_uom,
                quoted_quantity=rfq_line.requested_quantity,
                rfq_line=rfq_line
            )
        else:
            uom_result = UOMConversionResult(
                is_compatible=False,
                source_uom=quote_item.quoted_uom or "UNKNOWN",
                target_uom=rfq_line.requested_uom or "UNKNOWN",
                conversion_method="INCOMPATIBLE",
                error_reason="Missing UOM on RFQ line or quote item"
            )

        uom_factor = uom_result.conversion_factor if uom_result.is_compatible else Decimal("1.0")
        if not uom_result.is_compatible:
            issues.append(ComparisonIssue(
                code=ComparisonIssueCode.UOM_INCOMPATIBLE,
                severity=ComparisonIssueSeverity.BLOCKING,
                message=uom_result.error_reason or f"Incompatible UOM {quote_item.quoted_uom} vs {rfq_line.requested_uom}",
                supplier_id=supplier_id,
                rfq_line_id=rfq_line.rfq_line_id,
                field_name="quoted_uom"
            ))

        # 2. Required Quantity in Quoted Units
        # (e.g. if RFQ asks for 100 FT and 1 MTR = 3.28084 FT, qty in MTR = 100 / 3.28084)
        if rfq_line.requested_quantity is not None and rfq_line.requested_quantity > Decimal("0.0"):
            if uom_factor > Decimal("0.0"):
                rfq_qty_in_quoted_uom = (rfq_line.requested_quantity / uom_factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            else:
                rfq_qty_in_quoted_uom = rfq_line.requested_quantity
        else:
            rfq_qty_in_quoted_uom = None
            issues.append(ComparisonIssue(
                code=ComparisonIssueCode.QUANTITY_MISMATCH,
                severity=ComparisonIssueSeverity.BLOCKING,
                message=f"RFQ line '{rfq_line.rfq_line_id}' is missing required quantity.",
                supplier_id=supplier_id,
                rfq_line_id=rfq_line.rfq_line_id,
                field_name="requested_quantity"
            ))

        # 3. Volume Tier Selection
        selected_tier: Optional[PriceTier] = None
        tier_selection_reason: Optional[str] = None
        unit_price_quoted: Decimal = quote_item.unit_price

        if quote_item.price_tiers and rfq_qty_in_quoted_uom is not None:
            matched_tier = self._select_volume_tier(quote_item.price_tiers, rfq_qty_in_quoted_uom)
            if matched_tier:
                selected_tier = matched_tier
                unit_price_quoted = matched_tier.unit_price
                tier_selection_reason = (
                    f"Selected tier for quantity {rfq_qty_in_quoted_uom} {quote_item.quoted_uom} "
                    f"(Min: {matched_tier.min_qty}, Max: {matched_tier.max_qty or 'Unlimited'})"
                )
            else:
                tier_selection_reason = (
                    f"No applicable volume tier for required quantity {rfq_qty_in_quoted_uom} {quote_item.quoted_uom}"
                )
                issues.append(ComparisonIssue(
                    code=ComparisonIssueCode.TIER_NOT_APPLICABLE,
                    severity=ComparisonIssueSeverity.BLOCKING,
                    message=tier_selection_reason,
                    supplier_id=supplier_id,
                    rfq_line_id=rfq_line.rfq_line_id,
                    field_name="price_tiers"
                ))

        # 4. Pricing in Quoted Currency
        discount_pct = quote_item.discount_pct
        net_unit_price_quoted = quantize_currency(
            unit_price_quoted * (Decimal("1.0") - (discount_pct / Decimal("100.0")))
        )
        if rfq_qty_in_quoted_uom is not None and rfq_line.requested_quantity is not None:
            line_taxable_quoted = quantize_currency(net_unit_price_quoted * rfq_qty_in_quoted_uom)
            tax_rate_pct = quote_item.tax_rate_pct
            tax_amount_quoted = quantize_currency(line_taxable_quoted * (tax_rate_pct / Decimal("100.0")))
            line_landed_base_item = line_taxable_quoted + tax_amount_quoted
            line_landed_cost_quoted = line_landed_base_item + allocated_charge_quoted
            unit_landed_price_quoted = (line_landed_cost_quoted / rfq_line.requested_quantity).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        else:
            line_taxable_quoted = Decimal("0.00")
            tax_amount_quoted = Decimal("0.00")
            line_landed_cost_quoted = Decimal("0.00")
            unit_landed_price_quoted = Decimal("0.0000")

        # 5. Currency Normalization
        exchange_rate: Optional[Decimal] = None
        exchange_rate_source: Optional[str] = None
        unit_landed_price_base: Optional[Decimal] = None
        line_total_landed_base: Optional[Decimal] = None

        if quoted_currency == base_curr:
            exchange_rate = Decimal("1.0")
            exchange_rate_source = "BASE_CURRENCY"
            unit_landed_price_base = unit_landed_price_quoted
            line_total_landed_base = quantize_currency(line_landed_cost_quoted)
        else:
            exchange_rate = exchange_rates.get(quoted_currency) if exchange_rates else None
            if exchange_rate and exchange_rate > Decimal("0.0"):
                exchange_rate_source = "CONFIGURED_RATE"
                unit_landed_price_base = (unit_landed_price_quoted * exchange_rate).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
                line_total_landed_base = quantize_currency(line_landed_cost_quoted * exchange_rate)
            else:
                issues.append(ComparisonIssue(
                    code=ComparisonIssueCode.MISSING_EXCHANGE_RATE,
                    severity=ComparisonIssueSeverity.BLOCKING,
                    message=f"Missing exchange rate from '{quoted_currency}' to base '{base_curr}'",
                    supplier_id=supplier_id,
                    rfq_line_id=rfq_line.rfq_line_id,
                    field_name="currency"
                ))

        # 6. Comparability Assessment
        match_status = matched_item.match_status
        is_comparable = False
        is_provisional = False

        has_blocking_issue = any(i.severity == ComparisonIssueSeverity.BLOCKING for i in issues)

        if match_status in (MatchStatus.EXACT_MATCH, MatchStatus.HIGH_CONFIDENCE_MATCH):
            if not has_blocking_issue:
                is_comparable = True
                is_provisional = False
        elif match_status == MatchStatus.REVIEW_REQUIRED:
            is_comparable = False
            is_provisional = True
            issues.append(ComparisonIssue(
                code=ComparisonIssueCode.REVIEW_REQUIRED_ITEM,
                severity=ComparisonIssueSeverity.WARNING,
                message=f"Match requires human review before automatic ranking: {', '.join(matched_item.review_reasons)}",
                supplier_id=supplier_id,
                rfq_line_id=rfq_line.rfq_line_id
            ))
        else:
            is_comparable = False
            is_provisional = False
            issues.append(ComparisonIssue(
                code=ComparisonIssueCode.UNMATCHED_ITEM if match_status == MatchStatus.UNMATCHED else ComparisonIssueCode.UOM_INCOMPATIBLE,
                severity=ComparisonIssueSeverity.BLOCKING,
                message=f"Item is {match_status.value}",
                supplier_id=supplier_id,
                rfq_line_id=rfq_line.rfq_line_id
            ))

        return NormalizedItemPrice(
            rfq_line_id=rfq_line.rfq_line_id,
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            matched_quote_item=matched_item,
            match_status=match_status,
            is_comparable=is_comparable,
            is_provisional=is_provisional,
            quoted_qty=quote_item.quoted_qty,
            quoted_uom=quote_item.quoted_uom,
            requested_qty=rfq_line.requested_quantity,
            requested_uom=rfq_line.requested_uom,
            uom_conversion_factor=uom_factor,
            selected_tier=selected_tier,
            tier_selection_reason=tier_selection_reason,
            unit_price_quoted=unit_price_quoted,
            discount_pct=discount_pct,
            net_unit_price_quoted=net_unit_price_quoted,
            tax_rate_pct=tax_rate_pct,
            tax_amount_quoted=tax_amount_quoted,
            allocated_charges_quoted=allocated_charge_quoted,
            charge_allocation_method=charge_allocation_method,
            line_landed_cost_quoted=line_landed_cost_quoted,
            unit_landed_price_quoted=unit_landed_price_quoted,
            quoted_currency=quoted_currency,
            base_currency=base_curr,
            exchange_rate=exchange_rate,
            exchange_rate_source=exchange_rate_source,
            unit_landed_price_base=unit_landed_price_base,
            line_total_landed_base=line_total_landed_base,
            lead_time_days=quote_item.lead_time_days or (submission.canonical_quote.delivery_terms.lead_time_days_default if submission.canonical_quote.delivery_terms else None),
            moq=quote_item.moq,
            issues=issues
        )

    def summarize_supplier_comparison(
        self,
        submission: SupplierQuoteSubmission,
        normalized_prices: List[NormalizedItemPrice],
        rfq_items_count: int,
        base_currency: str,
        exchange_rates: Dict[str, Decimal],
    ) -> SupplierComparison:
        """Assembles quote-level supplier comparison summary."""
        quote = submission.canonical_quote
        quoted_curr = quote.currency.upper().strip()
        base_curr = base_currency.upper().strip()
        issues: List[ComparisonIssue] = []

        # Exchange rate
        exchange_rate: Optional[Decimal] = None
        exchange_rate_source: Optional[str] = None
        if quoted_curr == base_curr:
            exchange_rate = Decimal("1.0")
            exchange_rate_source = "BASE_CURRENCY"
        elif exchange_rates and quoted_curr in exchange_rates and exchange_rates[quoted_curr] > Decimal("0.0"):
            exchange_rate = exchange_rates[quoted_curr]
            exchange_rate_source = "CONFIGURED_RATE"
        else:
            issues.append(ComparisonIssue(
                code=ComparisonIssueCode.MISSING_EXCHANGE_RATE,
                severity=ComparisonIssueSeverity.BLOCKING,
                message=f"Missing exchange rate from '{quoted_curr}' to base '{base_curr}'",
                supplier_id=submission.supplier_id
            ))

        # Commercial charges summary in quoted currency
        charges_map: Dict[str, Decimal] = {}
        total_charges_quoted = Decimal("0.0")
        for chg in quote.additional_charges:
            c_type = chg.charge_type.value if hasattr(chg.charge_type, "value") else str(chg.charge_type)
            charges_map[c_type] = chg.total_with_tax
            total_charges_quoted += chg.total_with_tax

        # Commercial terms
        pay_terms = quote.payment_terms
        del_terms = quote.delivery_terms

        comm_terms = SupplierCommercialTerms(
            payment_terms_raw=pay_terms.raw_text if pay_terms else None,
            advance_pct=pay_terms.advance_pct if pay_terms else None,
            credit_days=pay_terms.credit_days if pay_terms else None,
            incoterm=del_terms.incoterm if del_terms else None,
            default_lead_time_days=del_terms.lead_time_days_default if del_terms else None,
            charges_summary_quoted=charges_map,
            total_charges_quoted=total_charges_quoted,
            total_charges_base=quantize_currency(total_charges_quoted * exchange_rate) if exchange_rate else None
        )

        comparable_count = sum(1 for p in normalized_prices if p.is_comparable)
        matched_count = sum(1 for p in normalized_prices if p.match_status in (MatchStatus.EXACT_MATCH, MatchStatus.HIGH_CONFIDENCE_MATCH, MatchStatus.REVIEW_REQUIRED))

        # Incomplete RFQ coverage check
        if len(normalized_prices) < rfq_items_count:
            issues.append(ComparisonIssue(
                code=ComparisonIssueCode.INCOMPLETE_QUOTE,
                severity=ComparisonIssueSeverity.WARNING,
                message=f"Supplier quoted only {len(normalized_prices)} of {rfq_items_count} requested RFQ items",
                supplier_id=submission.supplier_id
            ))

        is_fully_comparable = (
            exchange_rate is not None
            and comparable_count == rfq_items_count
            and not any(i.severity == ComparisonIssueSeverity.BLOCKING for i in issues)
        )

        # Aggregated Financials in Base Currency (for comparable items)
        line_items_gross_base = Decimal("0.0") if exchange_rate else None
        line_items_net_base = Decimal("0.0") if exchange_rate else None
        line_items_tax_base = Decimal("0.0") if exchange_rate else None
        charges_net_base = Decimal("0.0") if exchange_rate else None
        charges_tax_base = Decimal("0.0") if exchange_rate else None

        if exchange_rate:
            for p in normalized_prices:
                if p.is_comparable and p.line_total_landed_base is not None:
                    # Item net & tax
                    net_base = quantize_currency(p.net_unit_price_quoted * (p.requested_qty / p.uom_conversion_factor) * exchange_rate)
                    tax_base = quantize_currency(p.tax_amount_quoted * exchange_rate)
                    line_items_net_base += net_base
                    line_items_tax_base += tax_base
                    line_items_gross_base += (net_base + tax_base)

            for chg in quote.additional_charges:
                net_chg_base = quantize_currency(chg.amount * exchange_rate)
                tax_chg_base = quantize_currency((chg.total_with_tax - chg.amount) * exchange_rate)
                charges_net_base += net_chg_base
                charges_tax_base += tax_chg_base

        total_quote_landed_base = None
        if exchange_rate and is_fully_comparable:
            total_quote_landed_base = line_items_gross_base + charges_net_base + charges_tax_base

        return SupplierComparison(
            supplier_id=submission.supplier_id,
            supplier_name=submission.supplier_name,
            quote_id=quote.quote_id,
            source_currency=quoted_curr,
            base_currency=base_curr,
            exchange_rate=exchange_rate,
            exchange_rate_source=exchange_rate_source,
            total_items_in_quote=len(quote.items),
            matched_rfq_items_count=matched_count,
            comparable_items_count=comparable_count,
            is_fully_comparable=is_fully_comparable,
            line_items_gross_base=line_items_gross_base,
            line_items_net_base=line_items_net_base,
            line_items_tax_base=line_items_tax_base,
            charges_net_base=charges_net_base,
            charges_tax_base=charges_tax_base,
            total_quote_landed_base=total_quote_landed_base,
            commercial_terms=comm_terms,
            issues=issues
        )

    def _select_volume_tier(self, tiers: List[PriceTier], qty: Decimal) -> Optional[PriceTier]:
        """Finds applicable volume tier for quantity."""
        for tier in tiers:
            if tier.min_qty <= qty:
                if tier.max_qty is None or qty <= tier.max_qty:
                    return tier
        return None
