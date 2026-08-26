"""
Sourcing Optimization Domain Contracts & Data Models.
Establishes canonical schemas for Procurement Strategies, Hard Constraints,
Supplier Evidence, Sourcing Scenarios, and Optimization Reports.

INVARIANTS:
- Pure Decimal arithmetic for all currency and quantitative values.
- Explicit semantic quantity provenance (RFQ_REQUIRED_QTY, SUPPLIER_QUOTED_QTY, AWARDED_QTY, UNUSED_QUOTE_QTY).
- Never fabricates missing evidence (marks as None / Unavailable).
"""

from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field


class ProcurementStrategy(str, Enum):
    COST_OPTIMIZED = "COST_OPTIMIZED"
    BALANCED = "BALANCED"
    DELIVERY_PRIORITY = "DELIVERY_PRIORITY"
    QUALITY_PRIORITY = "QUALITY_PRIORITY"
    SUPPLIER_RISK_PRIORITY = "SUPPLIER_RISK_PRIORITY"
    BUYER_DEFINED = "BUYER_DEFINED"


class StrategyWeights(BaseModel):
    cost_weight: float = 0.85
    delivery_weight: float = 0.05
    quality_weight: float = 0.05
    risk_weight: float = 0.05


class HardConstraintCode(str, Enum):
    UOM_INCOMPATIBLE = "UOM_INCOMPATIBLE"
    BLOCKED_CONFLICT = "BLOCKED_CONFLICT"
    SUPPLIER_INACTIVE = "SUPPLIER_INACTIVE"
    ZERO_CAPACITY = "ZERO_CAPACITY"
    PRICE_CEILING_EXCEEDED = "PRICE_CEILING_EXCEEDED"
    LEAD_TIME_EXCEEDED = "LEAD_TIME_EXCEEDED"
    MOQ_VIOLATION = "MOQ_VIOLATION"
    MISSING_REQUIRED_PRICE = "MISSING_REQUIRED_PRICE"


class SupplierConstraintEvaluation(BaseModel):
    supplier_id: str
    supplier_name: str
    rfq_line_id: str
    is_eligible: bool = True
    violations: List[HardConstraintCode] = Field(default_factory=list)
    violation_reasons: List[str] = Field(default_factory=list)
    explanations: List[str] = Field(default_factory=list)


class SupplierEvidence(BaseModel):
    supplier_id: str
    supplier_name: str
    rfq_line_id: str
    unit_landed_price_base: Optional[Decimal] = None
    quoted_capacity: Optional[Decimal] = None
    quoted_uom: Optional[str] = None
    lead_time_days: Optional[int] = None
    moq: Optional[Decimal] = None
    spec_match_score: Optional[float] = None
    quality_rating: Optional[float] = None
    payment_terms_raw: Optional[str] = None
    credit_days: Optional[int] = None
    advance_pct: Optional[Decimal] = None
    
    # Evidence availability flags (strictly tracked, no fabrication)
    has_lead_time: bool = False
    has_quality_rating: bool = False
    has_spec_match: bool = False
    has_payment_terms: bool = False


class LineSplitPlan(BaseModel):
    """Concrete allocation assigned to a supplier for a specific RFQ Line."""
    supplier_id: str
    supplier_name: str
    quote_id: str
    awarded_qty: Decimal = Decimal("0")
    quoted_capacity: Optional[Decimal] = None
    quoted_uom: str = "PCS"
    converted_capacity: Optional[Decimal] = None
    converted_uom: Optional[str] = None
    uom_conversion_factor: Decimal = Decimal("1.0")
    uom_conversion_formula: Optional[str] = None
    unit_landed_cost: Decimal = Decimal("0.0")
    split_value: Decimal = Decimal("0.0")
    unused_quote_qty: Optional[Decimal] = None
    unused_converted_qty: Optional[Decimal] = None
    is_l1_for_line: bool = False
    allocation_reason: str = ""


class SourcingScenario(BaseModel):
    """Complete, feasible procurement sourcing plan across all RFQ lines."""
    scenario_id: str
    scenario_name: str
    scenario_type: str  # COST_OPTIMIZED_SPLIT | SINGLE_SUPPLIER_L1 | DELIVERY_PRIORITY | BALANCED_SPLIT | ALTERNATIVE_SINGLE | MANUAL
    is_recommended: bool = False
    recommendation_rank: int = 1
    total_cost_base: Decimal = Decimal("0.0")
    cost_delta_vs_recommended: Decimal = Decimal("0.0")
    cost_delta_pct_vs_recommended: Decimal = Decimal("0.0")
    fulfillment_pct: Decimal = Decimal("0.0")
    fully_fulfilled_lines: int = 0
    partially_fulfilled_lines: int = 0
    unallocated_lines: int = 0
    total_awarded_items: int = 0
    
    # Supplier summary: supplier_id -> {"supplier_name": str, "awarded_qty": Decimal, "share_pct": Decimal, "total_value": Decimal}
    supplier_allocations_summary: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    
    # Line plans: rfq_line_id -> List[LineSplitPlan]
    line_plans: Dict[str, List[LineSplitPlan]] = Field(default_factory=dict)
    
    # Objective / evidence scores
    composite_score: float = 0.0
    cost_score: float = 0.0
    delivery_score: Optional[float] = None
    quality_score: Optional[float] = None
    risk_score: Optional[float] = None
    
    explanation: str = ""
    evidence_summary: List[str] = Field(default_factory=list)
    constraints_status: str = "ALL_HARD_CONSTRAINTS_SATISFIED"
    advantages: List[str] = Field(default_factory=list)
    disadvantages: List[str] = Field(default_factory=list)


class LineAlternativeOption(BaseModel):
    """Feasible alternative supplier allocation option for a single RFQ line."""
    option_id: str
    option_type: str  # "SINGLE_SUPPLIER" | "SPLIT_SOURCING" | "PARTIAL_FULFILLMENT" | "UNALLOCATED"
    category: str = "FULL_FULFILLMENT"  # "FULL_FULFILLMENT" | "PARTIAL_FULFILLMENT"
    title: str
    supplier_names: List[str] = Field(default_factory=list)
    splits: List[LineSplitPlan] = Field(default_factory=list)
    total_allocated_qty: Decimal = Decimal("0")
    total_line_value: Decimal = Decimal("0.0")
    cost_delta_vs_recommended: Decimal = Decimal("0.0")
    cost_delta_pct_vs_recommended: Decimal = Decimal("0.0")
    fulfillment_pct: Decimal = Decimal("0.0")
    remaining_qty: Decimal = Decimal("0.0")
    shortfall_qty: Decimal = Decimal("0.0")
    is_recommended: bool = False
    rank: int = 1
    feasibility_status: str = "FEASIBLE"  # "FEASIBLE" | "PARTIAL" | "SHORTFALL"
    reason: str = ""
    explanation: str = ""


class LineRecommendation(BaseModel):
    """Authoritative server-side recommended allocation and ranked alternatives for a single RFQ Line."""
    rfq_line_id: str
    item_sku: str
    item_description: str
    strategy: ProcurementStrategy = ProcurementStrategy.COST_OPTIMIZED
    strategy_label: str = "Cost Optimized"
    required_qty: Optional[Decimal] = None
    required_uom: Optional[str] = "PCS"
    
    recommended_option_type: str = "SINGLE_SUPPLIER"  # "SINGLE_SUPPLIER" | "SPLIT_SOURCING" | "PARTIAL_FULFILLMENT" | "UNALLOCATED"
    recommended_splits: List[LineSplitPlan] = Field(default_factory=list)
    total_allocated_qty: Decimal = Decimal("0")
    remaining_qty: Decimal = Decimal("0")
    shortfall_qty: Decimal = Decimal("0")
    fulfillment_pct: Decimal = Decimal("0.0")
    total_line_value: Decimal = Decimal("0.0")
    
    is_supply_shortfall: bool = False
    aggregate_eligible_capacity: Optional[Decimal] = None
    
    rationale: str = ""
    why_recommended_breakdown: Dict[str, Any] = Field(default_factory=dict)
    
    alternatives: List[LineAlternativeOption] = Field(default_factory=list)
    full_fulfillment_options: List[LineAlternativeOption] = Field(default_factory=list)
    partial_fulfillment_options: List[LineAlternativeOption] = Field(default_factory=list)
    
    supplier_exclusion_reasons: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class SourcingOptimizationReport(BaseModel):
    """Authoritative output of the Sourcing Optimization Engine."""
    rfq_id: str
    comparison_id: str
    base_currency: str = "INR"
    strategy: ProcurementStrategy = ProcurementStrategy.COST_OPTIMIZED
    strategy_description: str = ""
    
    recommended_scenario: Optional[SourcingScenario] = None
    alternative_scenarios: List[SourcingScenario] = Field(default_factory=list)
    all_scenarios: List[SourcingScenario] = Field(default_factory=list)
    
    line_recommendations: Dict[str, LineRecommendation] = Field(default_factory=dict)
    
    ineligible_suppliers: Dict[str, List[str]] = Field(default_factory=dict)  # supplier_id -> reasons
    evidence_availability: Dict[str, bool] = Field(default_factory=dict)
    
    generated_at: str = ""

