"""
Matching Domain Contracts & Data Models.
Exposes stable schemas for RFQ Lines, Item Master Records, Match Candidates,
UOM Conversion Results, and MatchedQuoteItems.
INVARIANT: Original extracted QuoteItem data is NEVER warped or overwritten.
"""

from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from core.canonical_quote import Provenance, QuoteItem


class MatchStatus(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    HIGH_CONFIDENCE_MATCH = "HIGH_CONFIDENCE_MATCH"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNMATCHED = "UNMATCHED"
    UOM_INCOMPATIBLE = "UOM_INCOMPATIBLE"


class DecisionBand(str, Enum):
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"          # >= 95%, auto-resolved if no conflicts
    BULK_CANDIDATE = "BULK_CANDIDATE"            # 80% - 94%, eligible for 2-step bulk confirmation
    MANUAL_REVIEW = "MANUAL_REVIEW"              # 50% - 79%, guided individual review
    LOW_CONFIDENCE = "LOW_CONFIDENCE"            # < 50%, no bulk resolve, individual review
    BLOCKED_CONFLICT = "BLOCKED_CONFLICT"        # Hard conflict overrides score


class EvidenceItem(BaseModel):
    signal: str                                  # e.g. "SUPPLIER_PN", "DESCRIPTION", "BRAND", "SPEC", "UOM", "MEMORY"
    status: str                                  # "PASS" | "WARNING" | "FAIL" | "INFO"
    description: str                             # Human-readable explanation


class MatchMethod(str, Enum):
    HISTORICAL_SUPPLIER_MAPPING = "HISTORICAL_SUPPLIER_MAPPING"
    SUPPLIER_SKU_EXACT = "SUPPLIER_SKU_EXACT"
    MANUFACTURER_PN_EXACT = "MANUFACTURER_PN_EXACT"
    INTERNAL_SKU_EXACT = "INTERNAL_SKU_EXACT"
    FUZZY_DESCRIPTION_MULTI_SIGNAL = "FUZZY_DESCRIPTION_MULTI_SIGNAL"
    NO_MATCH = "NO_MATCH"


class RFQLineItem(BaseModel):
    rfq_line_id: str
    internal_item_id: Optional[str] = None
    sku: Optional[str] = None
    description: str
    category: Optional[str] = None
    manufacturer: Optional[str] = None
    manufacturer_part_number: Optional[str] = None
    supplier_part_number: Optional[str] = None
    specifications: Dict[str, str] = Field(default_factory=dict)
    requested_quantity: Optional[Decimal] = Field(default=None, gt=Decimal("0.0"))
    requested_uom: Optional[str] = None
    target_unit_price: Optional[Decimal] = None
    approved_uom_conversions: Dict[str, Decimal] = Field(default_factory=dict)


class ItemStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class ItemMasterRecord(BaseModel):
    internal_item_id: str
    internal_sku: str
    category: Optional[str] = None
    manufacturer: Optional[str] = None
    manufacturer_part_number: Optional[str] = None
    approved_supplier_part_numbers: List[str] = Field(default_factory=list)
    canonical_description: str
    stocking_uom: str
    approved_conversion_factors: Dict[str, Decimal] = Field(default_factory=dict)
    brand: Optional[str] = None
    specifications: Dict[str, str] = Field(default_factory=dict)
    status: str = "ACTIVE"  # "ACTIVE" | "INACTIVE"
    import_batch_id: Optional[str] = None
    created_at: Optional[str] = None


class SupplierNameChange(BaseModel):
    old_name: str
    new_name: str
    changed_at: str
    changed_by: str = "SYSTEM"


class SupplierMasterRecord(BaseModel):
    supplier_id: str
    supplier_name: str
    status: str = "ACTIVE"  # "ACTIVE" | "INACTIVE"
    name_history: List[SupplierNameChange] = Field(default_factory=list)
    created_at: str


class SupplierMappingRecord(BaseModel):
    supplier_id: str
    supplier_name: Optional[str] = None
    supplier_part_number: str
    internal_sku: str
    internal_item_id: Optional[str] = None
    canonical_description: Optional[str] = None
    confirmed_by: str = "Procurement Specialist"
    confirmed_at: str
    source_rfq_id: Optional[str] = None
    source_quote_id: Optional[str] = None


class ImportBatchRecord(BaseModel):
    import_batch_id: str
    filename: str
    imported_at: str
    item_ids: List[str] = Field(default_factory=list)
    items_count: int = 0
    status: str = "COMPLETED"  # "COMPLETED" | "UNDONE"
    source_type: str = "RFQ_REQUIREMENTS"  # "RFQ_REQUIREMENTS" | "CATALOG_IMPORT"


class MatchCandidate(BaseModel):
    candidate_item_id: str
    candidate_sku: str
    candidate_description: str
    match_method: MatchMethod
    match_score: float = Field(ge=0.0, le=1.0)
    match_status: MatchStatus
    decision_band: Optional[str] = None          # HIGH_CONFIDENCE | BULK_CANDIDATE | MANUAL_REVIEW | LOW_CONFIDENCE | BLOCKED_CONFLICT
    explanations: List[str] = Field(default_factory=list)
    evidence_checklist: List[EvidenceItem] = Field(default_factory=list)
    matched_fields: List[str] = Field(default_factory=list)
    has_hard_conflict: bool = False
    conflict_reasons: List[str] = Field(default_factory=list)
    source_provenance: Optional[Provenance] = None


class UOMConversionResult(BaseModel):
    is_compatible: bool
    conversion_factor: Decimal = Decimal("1.0")
    converted_quantity: Optional[Decimal] = None
    source_uom: str
    target_uom: str
    conversion_method: str   # "EXACT_UOM_MATCH" | "STANDARD_DIMENSION" | "ITEM_MASTER_FACTOR" | "INCOMPATIBLE"
    error_reason: Optional[str] = None


class MatchedQuoteItem(BaseModel):
    quote_item: QuoteItem                             # Original extracted item (never mutated)
    rfq_match: Optional[MatchCandidate] = None
    item_master_match: Optional[MatchCandidate] = None
    uom_conversion: Optional[UOMConversionResult] = None
    match_status: MatchStatus = MatchStatus.UNMATCHED
    decision_band: Optional[str] = None               # DecisionBand string
    has_hard_conflict: bool = False
    conflict_reasons: List[str] = Field(default_factory=list)
    evidence_checklist: List[EvidenceItem] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)
    top_candidates: List[MatchCandidate] = Field(default_factory=list)
