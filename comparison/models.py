"""
Comparison Domain Contracts & Data Models.
Defines schemas for RFQ Comparisons, Supplier Comparisons, Normalized Item Prices,
Commercial Terms, and Structured Comparison Issues.
INVARIANTS:
- Pure Decimal arithmetic for all currency and quantitative values.
- Never mutates CanonicalQuote or QuoteItem.
- Preserves full provenance, calculation methods, exchange rates, and issues.
"""

from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from core.canonical_quote import CanonicalQuote, PriceTier, Provenance, QuoteItem
from matching.models import MatchCandidate, MatchedQuoteItem, MatchStatus, RFQLineItem


class ComparisonIssueSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    BLOCKING = "BLOCKING"


class ComparisonIssueCode(str, Enum):
    MISSING_EXCHANGE_RATE = "MISSING_EXCHANGE_RATE"
    UOM_INCOMPATIBLE = "UOM_INCOMPATIBLE"
    UNMATCHED_ITEM = "UNMATCHED_ITEM"
    REVIEW_REQUIRED_ITEM = "REVIEW_REQUIRED_ITEM"
    TIER_NOT_APPLICABLE = "TIER_NOT_APPLICABLE"
    MISSING_COMMERCIAL_DATA = "MISSING_COMMERCIAL_DATA"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    MISSING_CHARGE_DATA = "MISSING_CHARGE_DATA"
    INCOMPLETE_QUOTE = "INCOMPLETE_QUOTE"


class ComparisonIssue(BaseModel):
    code: ComparisonIssueCode
    severity: ComparisonIssueSeverity
    message: str
    supplier_id: Optional[str] = None
    rfq_line_id: Optional[str] = None
    field_name: Optional[str] = None


class ChargeAllocationMethod(str, Enum):
    NONE = "NONE"                                  # Quote-level only, line items have 0 allocated charges
    PROPORTIONAL_LINE_VALUE = "PROPORTIONAL_LINE_VALUE"  # Allocated based on line taxable amount ratio
    PROPORTIONAL_QUANTITY = "PROPORTIONAL_QUANTITY"      # Allocated based on requested item quantity ratio


class CommercialValue(BaseModel):
    supplier_id: str
    supplier_name: str
    quote_id: str
    original_amount: Decimal
    original_currency: str
    exchange_rate: Decimal = Decimal("1.0")
    exchange_rate_source: str = "BASE_CURRENCY"
    normalized_amount: Decimal
    base_currency: str
    calculation_method: str
    provenance: Optional[Provenance] = None


class NormalizedItemPrice(BaseModel):
    """
    Normalized price calculation for a single RFQ item from a single supplier quote.
    Evaluated for the requested RFQ quantity and converted into base currency.
    """
    rfq_line_id: str
    supplier_id: str
    supplier_name: str
    matched_quote_item: MatchedQuoteItem
    match_status: MatchStatus

    # Flags
    is_comparable: bool = False
    is_provisional: bool = False

    # Quantity & UOM
    quoted_qty: Decimal
    quoted_uom: str
    requested_qty: Optional[Decimal] = None
    requested_uom: Optional[str] = None
    uom_conversion_factor: Decimal = Decimal("1.0")  # target_units = quoted_units * factor

    # Tier Pricing metadata
    selected_tier: Optional[PriceTier] = None
    tier_selection_reason: Optional[str] = None

    # Pricing components in quoted currency (for requested RFQ quantity)
    unit_price_quoted: Decimal
    discount_pct: Decimal = Decimal("0.0")
    net_unit_price_quoted: Decimal
    tax_rate_pct: Decimal = Decimal("0.0")
    tax_amount_quoted: Decimal = Decimal("0.0")
    allocated_charges_quoted: Decimal = Decimal("0.0")
    charge_allocation_method: ChargeAllocationMethod = ChargeAllocationMethod.NONE
    line_landed_cost_quoted: Decimal
    unit_landed_price_quoted: Decimal  # Landed cost per requested RFQ unit in quoted currency

    # Currency normalization to base currency
    quoted_currency: str
    base_currency: str
    exchange_rate: Optional[Decimal] = None
    exchange_rate_source: Optional[str] = None
    unit_landed_price_base: Optional[Decimal] = None  # in base currency
    line_total_landed_base: Optional[Decimal] = None  # in base currency

    # Delivery & Commercial signals
    lead_time_days: Optional[int] = None
    moq: Optional[Decimal] = None

    issues: List[ComparisonIssue] = Field(default_factory=list)


class ItemComparison(BaseModel):
    """Comparison across all suppliers for a specific RFQ Line Item."""
    rfq_line_id: str
    item_description: str
    requested_quantity: Optional[Decimal] = None
    requested_uom: Optional[str] = None
    supplier_prices: Dict[str, NormalizedItemPrice] = Field(default_factory=dict)
    comparable_supplier_ids: List[str] = Field(default_factory=list)
    provisional_supplier_ids: List[str] = Field(default_factory=list)
    issues: List[ComparisonIssue] = Field(default_factory=list)


class SupplierCommercialTerms(BaseModel):
    """Normalized commercial summary for a supplier."""
    payment_terms_raw: Optional[str] = None
    advance_pct: Optional[Decimal] = None
    credit_days: Optional[int] = None
    incoterm: Optional[str] = None
    default_lead_time_days: Optional[int] = None
    charges_summary_quoted: Dict[str, Decimal] = Field(default_factory=dict)
    total_charges_quoted: Decimal = Decimal("0.0")
    total_charges_base: Optional[Decimal] = None


class SupplierComparison(BaseModel):
    """Supplier-level quotation comparison summary."""
    supplier_id: str
    supplier_name: str
    quote_id: str
    source_currency: str
    base_currency: str
    exchange_rate: Optional[Decimal] = None
    exchange_rate_source: Optional[str] = None

    total_items_in_quote: int = 0
    matched_rfq_items_count: int = 0
    comparable_items_count: int = 0
    is_fully_comparable: bool = False

    # Financials in base currency
    line_items_gross_base: Optional[Decimal] = None
    line_items_net_base: Optional[Decimal] = None
    line_items_tax_base: Optional[Decimal] = None
    charges_net_base: Optional[Decimal] = None
    charges_tax_base: Optional[Decimal] = None
    total_quote_landed_base: Optional[Decimal] = None

    commercial_terms: SupplierCommercialTerms = Field(default_factory=SupplierCommercialTerms)
    issues: List[ComparisonIssue] = Field(default_factory=list)


class RFQStatus(str, Enum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    QUOTATIONS_RECEIVED = "QUOTATIONS_RECEIVED"
    UNDER_REVIEW = "UNDER_REVIEW"
    COMPARISON_READY = "COMPARISON_READY"
    AWARD_DRAFT = "AWARD_DRAFT"
    AWARDED = "AWARDED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class RFQDocument(BaseModel):
    """Enterprise RFQ Request envelope with immutable identity and permanent lifecycle persistence."""
    rfq_id: str
    title: str
    base_currency: str = "INR"
    status: str = "DRAFT"  # "DRAFT" | "OPEN" | "AWARDED" | "CLOSED" | "CANCELLED" | "ARCHIVED"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    archived_at: Optional[str] = None
    items: List[RFQLineItem] = Field(default_factory=list)



class SupplierQuoteSubmission(BaseModel):
    """Container pairing a CanonicalQuote with its MatchedQuoteItems."""
    supplier_id: str
    supplier_name: str
    canonical_quote: CanonicalQuote
    matched_items: List[MatchedQuoteItem]


class RFQComparison(BaseModel):
    """Full auditable comparison matrix across all RFQ items and suppliers."""
    rfq_id: str
    base_currency: str
    charge_allocation_method: ChargeAllocationMethod
    exchange_rates_used: Dict[str, Decimal] = Field(default_factory=dict)
    suppliers: Dict[str, SupplierComparison] = Field(default_factory=dict)
    item_comparisons: Dict[str, ItemComparison] = Field(default_factory=dict)
    global_issues: List[ComparisonIssue] = Field(default_factory=list)
