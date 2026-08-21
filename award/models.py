"""
Award Domain Models & Decision Contracts.
Supports multi-supplier split sourcing per line, scenario pre-population,
visual quantity fulfillment validation, unallocated item warnings, and two-step finalization.
"""

from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class AwardStatus(str, Enum):
    DRAFT = "DRAFT"
    FINALIZED = "FINALIZED"
    REOPENED = "REOPENED"


class AwardScenarioType(str, Enum):
    SINGLE_SUPPLIER_L1 = "SINGLE_SUPPLIER_L1"
    LINE_ITEM_OPTIMAL = "LINE_ITEM_OPTIMAL"
    MANUAL_ALLOCATION = "MANUAL_ALLOCATION"


class SupplierBidOption(BaseModel):
    supplier_id: str
    supplier_name: str
    quote_id: str
    quoted_qty: Decimal
    quoted_uom: str
    unit_landed_cost: Decimal
    currency: str
    is_l1_for_line: bool = False
    is_partial_quote: bool = False


class SupplierSplitAllocation(BaseModel):
    supplier_id: str
    supplier_name: str
    quote_id: str
    allocated_qty: Decimal = Decimal("0")
    quoted_uom: str = "PCS"
    unit_landed_cost: Decimal = Decimal("0")
    split_value: Decimal = Decimal("0")
    is_l1_for_line: bool = False
    quoted_capacity: Optional[Decimal] = None


class AwardLineAllocation(BaseModel):
    rfq_line_id: str
    item_sku: str
    item_description: str
    required_qty: Decimal
    required_uom: str
    total_allocated_qty: Decimal = Decimal("0")
    unallocated_qty: Decimal = Decimal("0")
    total_line_value: Decimal = Decimal("0")
    allocation_state: str = "UNALLOCATED"  # FULLY_ALLOCATED | PARTIALLY_ALLOCATED | UNALLOCATED | OVER_ALLOCATED | NOT_QUOTED
    supplier_splits: List[SupplierSplitAllocation] = Field(default_factory=list)
    available_bids: List[SupplierBidOption] = Field(default_factory=list)


class AwardDecisionRecord(BaseModel):
    award_id: str
    rfq_id: str
    comparison_id: str
    status: AwardStatus = AwardStatus.DRAFT
    selected_scenario: str = AwardScenarioType.SINGLE_SUPPLIER_L1.value
    base_currency: str
    total_awarded_value: Decimal = Decimal("0")
    total_required_items: int = 0
    fully_allocated_items_count: int = 0
    partially_allocated_items_count: int = 0
    unallocated_items_count: int = 0
    has_unallocated_quantities: bool = False
    buyer_accepted_unallocated: bool = False
    allocations: List[AwardLineAllocation] = Field(default_factory=list)
    validation_passed: bool = True
    validation_errors: List[str] = Field(default_factory=list)
    validation_warnings: List[str] = Field(default_factory=list)
    awarded_by: str = "Procurement Specialist"
    awarded_at: Optional[str] = None
    award_notes: Optional[str] = None
