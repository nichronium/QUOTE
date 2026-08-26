"""
Explicit Award State Machine & Lifecycle Transition Guards.
"""

from typing import Set, Tuple
from award.models import AwardStatus, BuyerDecisionState


VALID_LIFECYCLE_TRANSITIONS: Set[Tuple[AwardStatus, AwardStatus]] = {
    (AwardStatus.DRAFT, AwardStatus.READY_FOR_FINALIZATION),
    (AwardStatus.DRAFT, AwardStatus.FINALIZED),
    (AwardStatus.READY_FOR_FINALIZATION, AwardStatus.FINALIZED),
    (AwardStatus.READY_FOR_FINALIZATION, AwardStatus.DRAFT),
    (AwardStatus.FINALIZED, AwardStatus.REOPENED),
    (AwardStatus.REOPENED, AwardStatus.READY_FOR_FINALIZATION),
    (AwardStatus.REOPENED, AwardStatus.FINALIZED),
    (AwardStatus.REOPENED, AwardStatus.DRAFT),
}


VALID_BUYER_DECISION_TRANSITIONS: Set[Tuple[BuyerDecisionState, BuyerDecisionState]] = {
    (BuyerDecisionState.SYSTEM_RECOMMENDED, BuyerDecisionState.BUYER_ACCEPTED),
    (BuyerDecisionState.SYSTEM_RECOMMENDED, BuyerDecisionState.BUYER_MODIFIED),
    (BuyerDecisionState.SYSTEM_RECOMMENDED, BuyerDecisionState.PARTIAL_ACKNOWLEDGED),
    (BuyerDecisionState.BUYER_ACCEPTED, BuyerDecisionState.BUYER_MODIFIED),
    (BuyerDecisionState.BUYER_MODIFIED, BuyerDecisionState.SYSTEM_RECOMMENDED),
    (BuyerDecisionState.BUYER_MODIFIED, BuyerDecisionState.BUYER_ACCEPTED),
    (BuyerDecisionState.PARTIAL_ACKNOWLEDGED, BuyerDecisionState.BUYER_MODIFIED),
    (BuyerDecisionState.PARTIAL_ACKNOWLEDGED, BuyerDecisionState.BUYER_ACCEPTED),
}


def can_transition_lifecycle(current: AwardStatus, target: AwardStatus) -> bool:
    if current == target:
        return True
    return (current, target) in VALID_LIFECYCLE_TRANSITIONS


def can_transition_decision(current: BuyerDecisionState, target: BuyerDecisionState) -> bool:
    if current == target:
        return True
    return (current, target) in VALID_BUYER_DECISION_TRANSITIONS
