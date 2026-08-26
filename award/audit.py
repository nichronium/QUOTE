"""
Append-Only Award Decision Audit Trail Engine.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AwardAuditEventType(str, Enum):
    PROPOSAL_GENERATED = "PROPOSAL_GENERATED"
    RECOMMENDATION_ACCEPTED = "RECOMMENDATION_ACCEPTED"
    ALLOCATION_MODIFIED = "ALLOCATION_MODIFIED"
    PARTIAL_AWARD_ACKNOWLEDGED = "PARTIAL_AWARD_ACKNOWLEDGED"
    ALTERNATIVE_APPLIED = "ALTERNATIVE_APPLIED"
    DRAFT_SAVED = "DRAFT_SAVED"
    AWARD_FINALIZATION_STARTED = "AWARD_FINALIZATION_STARTED"
    AWARD_FINALIZED = "AWARD_FINALIZED"
    AWARD_REOPENED = "AWARD_REOPENED"
    FINALIZATION_CANCELLED = "FINALIZATION_CANCELLED"


class AwardAuditEvent(BaseModel):
    event_id: str
    award_id: str
    rfq_id: str
    event_type: AwardAuditEventType
    performed_by: str = "Procurement Specialist"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    line_id: Optional[str] = None
    previous_state: Optional[str] = None
    resulting_state: Optional[str] = None
    reason: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
