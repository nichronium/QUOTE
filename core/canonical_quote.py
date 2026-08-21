"""
Canonical Quote Schema - Hardened V1 Production Contract
Uses Decimal for financial precision, supports field-level correction locks,
and preserves exact cell-level provenance coordinates for items and metadata.

V2 additions: FieldEvidence tracks raw/normalized value, extraction method,
evidence signals, confidence score, and semantic status (CONFIRMED/INFERRED/
AMBIGUOUS/MISSING) for every extracted document-level and item-level field.
"""

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field, model_validator


class FieldStatus(str, Enum):
    """Semantic confidence status for a single extracted field."""
    CONFIRMED = "CONFIRMED"    # Unambiguous evidence (explicit key-value pair, repeated metadata column)
    INFERRED = "INFERRED"      # Pattern match from free text or fallback heuristic
    AMBIGUOUS = "AMBIGUOUS"    # Multiple conflicting signals; human review recommended
    MISSING = "MISSING"        # Not found in document


class FieldEvidence(BaseModel):
    """
    Full provenance and confidence record for a single extracted field.
    Attached to document-level fields (supplier, quote_number, etc.)
    and optionally to item-level fields (description, qty, price, uom).
    """
    raw_value: Optional[str] = None                # Exact text from source document
    normalized_value: Optional[Any] = None         # Python-typed normalized value
    source_file: Optional[str] = None
    sheet_name: Optional[str] = None
    cell_range: Optional[str] = None               # e.g. "B2" or "B2:D2"
    extraction_method: Optional[str] = None        # "kv_pair"|"column_repeat"|"text_block"|"column_header"
    evidence_signals: List[str] = Field(default_factory=list)  # Human-readable evidence list
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: FieldStatus = FieldStatus.MISSING



class HiddenRowPolicy(str, Enum):
    SMART_INCLUDE = "SMART_INCLUDE"  # Default: filter helper/decoy rows, preserve genuine hidden items
    EXCLUDE_ALL = "EXCLUDE_ALL"      # Exclude all hidden rows
    FLAG_REVIEW = "FLAG_REVIEW"      # Include genuine hidden items and route document to REVIEW_REQUIRED


def quantize_currency(value: Decimal) -> Decimal:
    """Standard 2-decimal rounding for currency values."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)



class ChargeType(str, Enum):
    FREIGHT = "FREIGHT"
    PACKING = "PACKING"
    TOOLING = "TOOLING"
    INSURANCE = "INSURANCE"
    OTHER = "OTHER"


class MatchStatus(str, Enum):
    AUTO_MATCHED = "AUTO_MATCHED"
    FUZZY_MATCHED = "FUZZY_MATCHED"
    MANUAL_MATCHED = "MANUAL_MATCHED"
    UNMATCHED = "UNMATCHED"
    IGNORED = "IGNORED"


class ProcessingMode(str, Enum):
    CLOUD = "CLOUD"
    LOCAL = "LOCAL"
    HYBRID = "HYBRID"


class FieldCorrection(BaseModel):
    """Tracks manual human override for a single field."""
    field_name: str
    original_extracted_value: Optional[Any] = None
    corrected_value: Any
    corrected_by: str = "procurement_user"
    corrected_at: datetime = Field(default_factory=datetime.utcnow)
    notes: Optional[str] = None


class Provenance(BaseModel):
    """Exact cell and document provenance."""
    page_number: Optional[int] = Field(default=None, ge=1)
    sheet_name: Optional[str] = None
    row_idx: Optional[int] = None
    col_idx: Optional[int] = None
    cell_ref: Optional[str] = None  # e.g., "E2"
    source_text: Optional[str] = None  # Exact raw text from cell
    is_hidden_row: bool = False
    bbox: Optional[Tuple[float, float, float, float]] = Field(
        default=None,
        description="[x0, y0, x1, y1] normalized to [0, 1] for PDF documents"
    )


class PaymentTerms(BaseModel):
    raw_text: Optional[str] = None
    credit_days: Optional[int] = Field(default=None, ge=0)
    advance_pct: Optional[Decimal] = Field(default=None, ge=Decimal("0.0"), le=Decimal("100.0"))
    provenance: Optional[Provenance] = None
    credit_days_evidence: Optional[FieldEvidence] = None
    advance_pct_evidence: Optional[FieldEvidence] = None


class DeliveryTerms(BaseModel):
    incoterm: Optional[str] = None
    dispatch_location: Optional[str] = None
    lead_time_days_default: Optional[int] = Field(default=None, ge=0)
    raw_text: Optional[str] = None
    provenance: Optional[Provenance] = None
    lead_time_days_evidence: Optional[FieldEvidence] = None
    incoterm_evidence: Optional[FieldEvidence] = None


class TaxComponent(BaseModel):
    tax_type: str = "GST"  # "CGST", "SGST", "IGST", "VAT", "CESS", "TAX"
    rate_pct: Decimal = Field(default=Decimal("0.0"), ge=Decimal("0.0"))
    amount: Optional[Decimal] = None
    evidence: Optional[FieldEvidence] = None


class AdditionalCharge(BaseModel):
    charge_type: ChargeType
    amount: Decimal = Field(ge=Decimal("0.0"))
    tax_rate_pct: Decimal = Field(default=Decimal("0.0"), ge=Decimal("0.0"))
    tax_components: List[TaxComponent] = Field(default_factory=list)
    raw_text: Optional[str] = None
    provenance: Optional[Provenance] = None
    amount_evidence: Optional[FieldEvidence] = None
    charge_type_evidence: Optional[FieldEvidence] = None
    tax_rate_evidence: Optional[FieldEvidence] = None

    @property
    def total_with_tax(self) -> Decimal:
        multiplier = Decimal("1.0") + (self.tax_rate_pct / Decimal("100.0"))
        return quantize_currency(self.amount * multiplier)


class PriceTier(BaseModel):
    min_qty: Decimal = Field(ge=Decimal("0.0"))
    max_qty: Optional[Decimal] = Field(default=None, ge=Decimal("0.0"))
    unit_price: Decimal = Field(ge=Decimal("0.0"))
    provenance: Optional[Provenance] = None
    min_qty_evidence: Optional[FieldEvidence] = None
    max_qty_evidence: Optional[FieldEvidence] = None
    unit_price_evidence: Optional[FieldEvidence] = None

    @model_validator(mode="after")
    def validate_tier_range(self):
        if self.max_qty is not None and self.max_qty < self.min_qty:
            raise ValueError(f"max_qty ({self.max_qty}) cannot be less than min_qty ({self.min_qty})")
        return self


class QuoteItem(BaseModel):
    line_index: int = Field(ge=0)
    raw_description: str
    supplier_part_number: Optional[str] = None
    hsn_sac_code: Optional[str] = None
    quoted_qty: Decimal = Field(gt=Decimal("0.0"))
    quoted_uom: str
    unit_price: Decimal = Field(ge=Decimal("0.0"))
    discount_pct: Decimal = Field(default=Decimal("0.0"), ge=Decimal("0.0"), le=Decimal("100.0"))
    net_unit_price: Optional[Decimal] = None
    tax_rate_pct: Decimal = Field(default=Decimal("0.0"), ge=Decimal("0.0"))
    tax_components: List[TaxComponent] = Field(default_factory=list)
    lead_time_days: Optional[int] = Field(default=None, ge=0)
    moq: Optional[Decimal] = Field(default=None, ge=Decimal("0.0"))


    price_tiers: List[PriceTier] = Field(default_factory=list)

    match_status: MatchStatus = MatchStatus.UNMATCHED
    matched_rfq_item_id: Optional[str] = None
    matched_erp_item_code: Optional[str] = None
    uom_conversion_factor: Decimal = Field(default=Decimal("1.0"), gt=Decimal("0.0"))
    confidence_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    provenance: Optional[Provenance] = None

    # Field-level evidence for every QuoteItem field
    description_evidence: Optional[FieldEvidence] = None
    supplier_part_number_evidence: Optional[FieldEvidence] = None
    quoted_qty_evidence: Optional[FieldEvidence] = None
    quoted_uom_evidence: Optional[FieldEvidence] = None
    unit_price_evidence: Optional[FieldEvidence] = None
    discount_evidence: Optional[FieldEvidence] = None
    tax_rate_evidence: Optional[FieldEvidence] = None
    line_total_evidence: Optional[FieldEvidence] = None
    
    # Granular field-level overrides: field_name -> FieldCorrection
    field_corrections: Dict[str, FieldCorrection] = Field(default_factory=dict)


    @model_validator(mode="after")
    def compute_net_price(self):
        """Auto-computes net unit price if omitted."""
        if self.net_unit_price is None:
            multiplier = Decimal("1.0") - (self.discount_pct / Decimal("100.0"))
            self.net_unit_price = (self.unit_price * multiplier).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        return self

    def apply_human_override(self, field_name: str, new_value: Any, user: str = "user", notes: Optional[str] = None):
        """Applies a human correction and records field-level provenance."""
        if not hasattr(self, field_name):
            raise AttributeError(f"Invalid field: {field_name}")
        
        orig_val = getattr(self, field_name)
        setattr(self, field_name, new_value)
        self.field_corrections[field_name] = FieldCorrection(
            field_name=field_name,
            original_extracted_value=orig_val,
            corrected_value=new_value,
            corrected_by=user,
            notes=notes
        )

    def resolve_effective_price(self, target_qty: Optional[Decimal] = None) -> Decimal:
        """Resolves unit price against volume tiers for a target quantity."""
        qty = target_qty if target_qty is not None else self.quoted_qty
        if not self.price_tiers:
            return self.net_unit_price or self.unit_price

        for tier in sorted(self.price_tiers, key=lambda t: t.min_qty, reverse=True):
            if qty >= tier.min_qty:
                if tier.max_qty is None or qty <= tier.max_qty:
                    multiplier = Decimal("1.0") - (self.discount_pct / Decimal("100.0"))
                    return (tier.unit_price * multiplier).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

        return self.net_unit_price or self.unit_price

    @property
    def gross_amount(self) -> Decimal:
        return quantize_currency(self.quoted_qty * self.unit_price)

    @property
    def discount_amount(self) -> Decimal:
        gross = self.quoted_qty * self.unit_price
        return quantize_currency(gross * (self.discount_pct / Decimal("100.0")))

    @property
    def taxable_amount(self) -> Decimal:
        return quantize_currency(self.gross_amount - self.discount_amount)

    @property
    def tax_amount(self) -> Decimal:
        return quantize_currency(self.taxable_amount * (self.tax_rate_pct / Decimal("100.0")))

    def calculate_line_landed_cost(self, target_qty: Optional[Decimal] = None) -> Decimal:
        """Calculates line landed cost = taxable + tax."""
        if target_qty is not None:
            effective_price = self.resolve_effective_price(target_qty)
            gross = quantize_currency(target_qty * effective_price)
            disc = quantize_currency(gross * (self.discount_pct / Decimal("100.0")))
            taxable = gross - disc
            tax = quantize_currency(taxable * (self.tax_rate_pct / Decimal("100.0")))
            return quantize_currency(taxable + tax)

        return quantize_currency(self.taxable_amount + self.tax_amount)


class ExtractionMetadata(BaseModel):
    source_file_name: str
    source_file_hash: str
    parser_used: str
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
    overall_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    warnings: List[str] = Field(default_factory=list)
    processing_mode: Optional[ProcessingMode] = ProcessingMode.CLOUD


class CanonicalQuote(BaseModel):
    quote_id: str
    rfq_reference: Optional[str] = None
    supplier_raw_name: str
    supplier_matched_id: Optional[str] = None
    supplier_tax_id: Optional[str] = None
    quote_number: Optional[str] = None
    quote_date: Optional[date] = None
    valid_until: Optional[date] = None
    currency: str = Field(default="INR")
    exchange_rate_to_base: Decimal = Field(default=Decimal("1.0"), gt=Decimal("0.0"))

    payment_terms: Optional[PaymentTerms] = None
    delivery_terms: Optional[DeliveryTerms] = None
    additional_charges: List[AdditionalCharge] = Field(default_factory=list)
    items: List[QuoteItem] = Field(default_factory=list)
    extraction_metadata: ExtractionMetadata

    # Field-level evidence for document-level metadata fields (optional)
    supplier_evidence: Optional[FieldEvidence] = None
    quote_number_evidence: Optional[FieldEvidence] = None
    quote_date_evidence: Optional[FieldEvidence] = None
    currency_evidence: Optional[FieldEvidence] = None
    stated_subtotal_evidence: Optional[FieldEvidence] = None
    stated_tax_evidence: Optional[FieldEvidence] = None
    stated_grand_total_evidence: Optional[FieldEvidence] = None



    def calculate_total_landed_cost(self) -> Decimal:
        """Calculates full quote landed cost."""
        items_total = sum((item.calculate_line_landed_cost() for item in self.items), Decimal("0.0"))
        charges_total = sum((charge.total_with_tax for charge in self.additional_charges), Decimal("0.0"))
        return quantize_currency((items_total + charges_total) * self.exchange_rate_to_base)

    def merge_reextraction(self, new_quote: "CanonicalQuote") -> "CanonicalQuote":
        """
        Guarantees TEST-EXT-03 with FIELD-LEVEL precision:
        Updates extracted fields with fresh AI output, while preserving
        ONLY the specific fields the human manually corrected.
        """
        existing_items = {item.line_index: item for item in self.items}
        decimal_fields = {
            "quoted_qty", "unit_price", "discount_pct", "net_unit_price",
            "tax_rate_pct", "uom_conversion_factor", "moq"
        }

        updated_items = []
        for new_item in new_quote.items:
            if new_item.line_index in existing_items:
                existing_item = existing_items[new_item.line_index]
                
                for field_name, correction in existing_item.field_corrections.items():
                    val = correction.corrected_value
                    if field_name in decimal_fields and val is not None:
                        val = Decimal(str(val))
                    setattr(new_item, field_name, val)
                    new_item.field_corrections[field_name] = correction

            updated_items.append(new_item)

        new_quote.items = updated_items
        return new_quote
