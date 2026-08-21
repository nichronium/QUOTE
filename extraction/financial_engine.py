"""
Authoritative Unified Financial Engine.
Performs pure Decimal arithmetic for gross, discount, net, tax, line landed cost,
volume tier resolution, and document-level financial aggregation.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple

from core.canonical_quote import AdditionalCharge, PriceTier, QuoteItem


def quantize_currency(val: Decimal) -> Decimal:
    """Rounds to standard 2-decimal currency precision."""
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class FinancialEngine:
    """Deterministic financial math engine."""

    @staticmethod
    def calculate_line_landed_cost(
        qty: Decimal,
        unit_price: Decimal,
        discount_pct: Decimal = Decimal("0.0"),
        tax_rate_pct: Decimal = Decimal("0.0"),
        price_tiers: Optional[List[PriceTier]] = None,
        target_qty: Optional[Decimal] = None
    ) -> Tuple[Decimal, Decimal, Decimal, Decimal]:
        """
        Calculates:
        effective_price = resolve tier if target_qty else unit_price
        gross = (target_qty or qty) * effective_price
        net = gross * (1 - discount_pct / 100)
        tax = net * (tax_rate_pct / 100)
        landed = net + tax

        Returns: (effective_unit_price, net_subtotal, tax_amount, landed_cost)
        """
        effective_price = unit_price
        eval_qty = target_qty if target_qty is not None else qty
        if price_tiers and target_qty is not None:
            for tier in sorted(price_tiers, key=lambda t: t.min_qty, reverse=True):
                if eval_qty >= tier.min_qty:
                    if tier.max_qty is None or eval_qty <= tier.max_qty:
                        effective_price = tier.unit_price
                        break

        gross = (eval_qty * effective_price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        disc_multiplier = Decimal("1.0") - (discount_pct / Decimal("100.0"))
        net = (gross * disc_multiplier).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        tax = (net * (tax_rate_pct / Decimal("100.0"))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        landed = quantize_currency(net + tax)

        return effective_price, net, tax, landed

    @staticmethod
    def calculate_quote_total(
        items: List[QuoteItem],
        charges: List[AdditionalCharge],
        exchange_rate: Decimal = Decimal("1.0")
    ) -> Decimal:
        """Calculates total quote landed cost in base currency."""
        items_total = sum((item.calculate_line_landed_cost() for item in items), Decimal("0.0"))
        charges_total = sum((charge.total_with_tax for charge in charges), Decimal("0.0"))
        return quantize_currency((items_total + charges_total) * exchange_rate)
