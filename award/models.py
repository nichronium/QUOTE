"""
Canonical Award Domain Contracts & Decision Models.
Post-award authoritative business object: AwardRecord.
All downstream documents (PDF, Excel, CSV) are derived projections of AwardRecord.
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AwardStatus(str, Enum):
    DRAFT = "DRAFT"
    READY_FOR_FINALIZATION = "READY_FOR_FINALIZATION"
    FINALIZED = "FINALIZED"
    REOPENED = "REOPENED"


class BuyerDecisionState(str, Enum):
    SYSTEM_RECOMMENDED = "SYSTEM_RECOMMENDED"
    BUYER_ACCEPTED = "BUYER_ACCEPTED"
    BUYER_MODIFIED = "BUYER_MODIFIED"
    PARTIAL_ACKNOWLEDGED = "PARTIAL_ACKNOWLEDGED"


class LineAllocationState(str, Enum):
    FULLY_FULFILLED = "FULLY_FULFILLED"
    FULLY_ALLOCATED = "FULLY_ALLOCATED"
    PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED"
    PARTIALLY_ALLOCATED = "PARTIALLY_ALLOCATED"
    UNALLOCATED = "UNALLOCATED"
    SHORTFALL = "SHORTFALL"
    OVER_ALLOCATED = "OVER_ALLOCATED"
    QUANTITY_UNAVAILABLE = "QUANTITY_UNAVAILABLE"


class CommercialBreakdown(BaseModel):
    """
    Generic, domain-agnostic commercial cost component structure.
    Only components genuinely present in canonical source quotes are populated.
    """
    base_unit_price: Optional[Decimal] = None
    discount_pct: Optional[Decimal] = None
    net_unit_price: Optional[Decimal] = None
    tax_rate_pct: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    allocated_charges: Optional[Decimal] = None
    exchange_rate: Decimal = Decimal("1.0")
    quoted_currency: str = "INR"
    base_currency: str = "INR"
    unit_landed_cost: Decimal = Decimal("0")
    calculation_source: Optional[str] = None
    uom_conversion_factor: Decimal = Decimal("1.0")
    uom_conversion_formula: Optional[str] = None


class DecisionProvenance(BaseModel):
    """
    Canonical explanation of why an allocation or recommendation was selected.
    """
    objective: str = "Lowest Total Landed Procurement Cost"
    strategy_label: str = "Cost Optimized"
    is_split_sourcing: bool = False
    supplier_count: int = 1
    feasibility_checks: List[str] = Field(default_factory=list)
    allocation_rationale: Optional[str] = None
    benchmark_value: Optional[Decimal] = None
    alternative_delta_value: Optional[Decimal] = None
    supplier_exclusion_reasons: Dict[str, Any] = Field(default_factory=dict)


class SupplierAllocation(BaseModel):
    """
    Authoritative supplier award split allocation record.
    """
    supplier_id: str
    supplier_name: str
    quote_id: str
    awarded_qty: Decimal = Decimal("0")
    canonical_uom: str = "PCS"
    supplier_quoted_qty: Optional[Decimal] = None
    supplier_quoted_uom: Optional[str] = None
    converted_capacity: Optional[Decimal] = None
    unused_quote_qty: Optional[Decimal] = None
    unused_converted_qty: Optional[Decimal] = None
    unit_landed_cost: Decimal = Decimal("0")
    award_value: Decimal = Decimal("0")
    is_l1_for_line: bool = False
    commercial_breakdown: Optional[CommercialBreakdown] = None
    uom_conversion_factor: Decimal = Decimal("1.0")
    uom_conversion_formula: Optional[str] = None


class RecommendationSnapshot(BaseModel):
    """
    Immutable snapshot of the system-recommended allocation baseline.
    Preserved independently of subsequent buyer adjustments.
    """
    strategy: str = "LINE_ITEM_OPTIMAL"
    strategy_label: str = "Cost Optimized"
    total_recommended_value: Decimal = Decimal("0")
    fulfillment_pct: Decimal = Decimal("100.0")
    is_supply_shortfall: bool = False
    shortfall_qty: Decimal = Decimal("0")
    recommended_splits: List[Dict[str, Any]] = Field(default_factory=list)
    alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    why_recommended: Optional[DecisionProvenance] = None
    snapshot_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class BuyerDecision(BaseModel):
    """
    Audit snapshot of buyer interaction, modifications, or acknowledgments.
    """
    decision_state: BuyerDecisionState = BuyerDecisionState.SYSTEM_RECOMMENDED
    buyer_id: str = "Procurement Specialist"
    decision_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    recommendation_accepted: bool = False
    buyer_modified: bool = False
    partial_acknowledged: bool = False
    modification_reason: Optional[str] = None


class LineAward(BaseModel):
    """
    Canonical post-award line item decision.
    """
    rfq_line_id: str
    item_sku: str
    item_description: str
    required_qty: Optional[Decimal] = None
    canonical_uom: str = "PCS"
    supplier_allocations: List[SupplierAllocation] = Field(default_factory=list)
    awarded_qty: Decimal = Decimal("0")
    remaining_qty: Optional[Decimal] = None
    shortfall_qty: Optional[Decimal] = None
    excess_qty: Decimal = Decimal("0")
    fulfillment_percentage: Decimal = Decimal("0")
    allocation_state: LineAllocationState = LineAllocationState.UNALLOCATED
    recommendation_snapshot: Optional[RecommendationSnapshot] = None
    buyer_decision: BuyerDecision = Field(default_factory=BuyerDecision)
    partial_acknowledgement_acknowledged: bool = False
    partial_acknowledgement_buyer: Optional[str] = None
    partial_acknowledgement_timestamp: Optional[str] = None
    total_line_value: Decimal = Decimal("0")


class SupplierAwardSummary(BaseModel):
    """
    Supplier-level aggregation for commercial reporting and purchase requisitions.
    Enforces strict dimension-safe quantity accounting grouped by canonical UOM.
    """
    supplier_id: str
    supplier_name: str
    quote_id: str
    rfq_lines_awarded_count: int = 0
    split_allocations_count: int = 0
    quantities_by_uom: Dict[str, Decimal] = Field(default_factory=dict)
    total_awarded_value: Decimal = Decimal("0")
    percentage_share: Decimal = Decimal("0")


class AwardCommercialSummary(BaseModel):
    """
    Aggregate commercial and currency financial metrics.
    """
    base_currency: str = "INR"
    total_awarded_value: Decimal = Decimal("0")
    total_baseline_estimated_value: Optional[Decimal] = None
    total_cost_advantage_vs_single: Optional[Decimal] = None


class AwardFulfillmentSummary(BaseModel):
    """
    Scope fulfillment summary across all unique RFQ demand lines.
    """
    total_rfq_lines: int = 0
    fully_allocated_lines_count: int = 0
    partially_allocated_lines_count: int = 0
    unallocated_lines_count: int = 0
    overall_fulfillment_percentage: Decimal = Decimal("0")
    has_unallocated_quantities: bool = False
    buyer_accepted_unallocated: bool = False


class AwardRecord(BaseModel):
    """
    Authoritative Post-Award Domain Business Object.
    Immutable when status == FINALIZED.
    """
    award_id: str
    rfq_id: str
    comparison_id: str
    status: AwardStatus = AwardStatus.DRAFT
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finalized_at: Optional[str] = None
    buyer_id: str = "Procurement Specialist"
    sourcing_strategy: str = "LINE_ITEM_OPTIMAL"
    selected_scenario_id: Optional[str] = None
    
    line_awards: List[LineAward] = Field(default_factory=list)
    supplier_summary: List[SupplierAwardSummary] = Field(default_factory=list)
    commercial_summary: AwardCommercialSummary = Field(default_factory=AwardCommercialSummary)
    fulfillment_summary: AwardFulfillmentSummary = Field(default_factory=AwardFulfillmentSummary)
    exception_summary: List[str] = Field(default_factory=list)
    audit_trail: List[Dict[str, Any]] = Field(default_factory=list)
    
    award_notes: Optional[str] = None
    reopen_history: List[Dict[str, Any]] = Field(default_factory=list)
    audit_metadata: Dict[str, Any] = Field(default_factory=dict)


class AwardArtifactPolicy(BaseModel):
    """
    Tenant/Client configuration governing which post-award artifacts are enabled.
    """
    award_report: bool = True     # Executive PDF
    award_workbook: bool = True   # Multi-tab Excel
    erp_export: bool = True       # Flat CSV for ERP
    supplier_requisition: bool = True  # Supplier-specific requisitions
