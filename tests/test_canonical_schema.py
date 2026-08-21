from datetime import date, datetime
from decimal import Decimal
import pytest
from core.canonical_quote import (
    CanonicalQuote,
    QuoteItem,
    PriceTier,
    AdditionalCharge,
    ChargeType,
    MatchStatus,
    ExtractionMetadata,
)


def test_quote_item_net_price_and_landed_cost():
    """Verify net price auto-calculation and landed cost math with Decimal precision."""
    item = QuoteItem(
        line_index=0,
        raw_description="SKF Bearing 6205",
        quoted_qty=Decimal("10"),
        quoted_uom="PCS",
        unit_price=Decimal("100.00"),
        discount_pct=Decimal("10.0"),  # Net: 90.00
        tax_rate_pct=Decimal("18.0"),  # Tax: 16.20 per unit -> Landed unit: 106.20
    )

    assert item.net_unit_price == Decimal("90.0000")
    # Total for 10 units: 900.00 + 162.00 = 1062.00
    assert item.calculate_line_landed_cost() == Decimal("1062.00")


def test_quote_total_landed_cost_with_charges_and_exchange():
    """Verify quote-level landed cost with additional freight charges and currency exchange."""
    item1 = QuoteItem(
        line_index=0,
        raw_description="Stainless Steel Bolt M8",
        quoted_qty=Decimal("100"),
        quoted_uom="PCS",
        unit_price=Decimal("5.00"),
        discount_pct=Decimal("0.0"),
        tax_rate_pct=Decimal("18.0"),
    )  # 100 * 5 * 1.18 = 590.00

    item2 = QuoteItem(
        line_index=1,
        raw_description="Hex Nut M8",
        quoted_qty=Decimal("100"),
        quoted_uom="PCS",
        unit_price=Decimal("2.00"),
        discount_pct=Decimal("0.0"),
        tax_rate_pct=Decimal("18.0"),
    )  # 100 * 2 * 1.18 = 236.00

    freight = AdditionalCharge(
        charge_type=ChargeType.FREIGHT,
        amount=Decimal("100.00"),
        tax_rate_pct=Decimal("18.0"),
    )  # 100 * 1.18 = 118.00

    quote = CanonicalQuote(
        quote_id="Q-TEST-01",
        supplier_raw_name="Industrial Fasteners Co",
        currency="INR",
        items=[item1, item2],
        additional_charges=[freight],
        extraction_metadata=ExtractionMetadata(
            source_file_name="quote.pdf",
            source_file_hash="mock_hash_123",
            parser_used="docling_v1"
        )
    )

    # 590.00 + 236.00 + 118.00 = 944.00
    assert quote.calculate_total_landed_cost() == Decimal("944.00")


def test_volume_tier_resolution():
    """Verify tier pricing resolves correctly according to requested quantity."""
    item = QuoteItem(
        line_index=0,
        raw_description="Custom Flange",
        quoted_qty=Decimal("10"),
        quoted_uom="PCS",
        unit_price=Decimal("150.00"),  # Fallback
        price_tiers=[
            PriceTier(min_qty=Decimal("1.0"), max_qty=Decimal("50.0"), unit_price=Decimal("150.00")),
            PriceTier(min_qty=Decimal("51.0"), max_qty=Decimal("200.0"), unit_price=Decimal("120.00")),
            PriceTier(min_qty=Decimal("201.0"), max_qty=None, unit_price=Decimal("95.00")),
        ]
    )

    assert item.resolve_effective_price(Decimal("10")) == Decimal("150.0000")
    assert item.resolve_effective_price(Decimal("100")) == Decimal("120.0000")
    assert item.resolve_effective_price(Decimal("500")) == Decimal("95.0000")
