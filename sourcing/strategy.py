"""
Procurement Strategy Definitions and Objective Weight Registry.
Translates enterprise procurement strategies into explicit, explainable optimization weights.
"""

from typing import Dict
from sourcing.models import ProcurementStrategy, StrategyWeights


STRATEGY_REGISTRY: Dict[ProcurementStrategy, StrategyWeights] = {
    ProcurementStrategy.COST_OPTIMIZED: StrategyWeights(
        cost_weight=0.85,
        delivery_weight=0.05,
        quality_weight=0.05,
        risk_weight=0.05,
    ),
    ProcurementStrategy.BALANCED: StrategyWeights(
        cost_weight=0.40,
        delivery_weight=0.25,
        quality_weight=0.20,
        risk_weight=0.15,
    ),
    ProcurementStrategy.DELIVERY_PRIORITY: StrategyWeights(
        cost_weight=0.25,
        delivery_weight=0.60,
        quality_weight=0.10,
        risk_weight=0.05,
    ),
    ProcurementStrategy.QUALITY_PRIORITY: StrategyWeights(
        cost_weight=0.25,
        delivery_weight=0.10,
        quality_weight=0.60,
        risk_weight=0.05,
    ),
    ProcurementStrategy.SUPPLIER_RISK_PRIORITY: StrategyWeights(
        cost_weight=0.30,
        delivery_weight=0.10,
        quality_weight=0.10,
        risk_weight=0.50,
    ),
}

STRATEGY_DESCRIPTIONS: Dict[ProcurementStrategy, str] = {
    ProcurementStrategy.COST_OPTIMIZED: (
        "Focuses primarily on minimizing total landed procurement cost while ensuring basic constraint compliance."
    ),
    ProcurementStrategy.BALANCED: (
        "Balances total procurement cost (40%), delivery lead time (25%), specification confidence (20%), and supplier risk (15%)."
    ),
    ProcurementStrategy.DELIVERY_PRIORITY: (
        "Prioritizes suppliers with the shortest verified lead times (60%), followed by landed cost (25%)."
    ),
    ProcurementStrategy.QUALITY_PRIORITY: (
        "Prioritizes highest specification match and brand adherence (60%), followed by landed cost (25%)."
    ),
    ProcurementStrategy.SUPPLIER_RISK_PRIORITY: (
        "Prioritizes risk mitigation through supplier diversification and favorable credit terms (50%)."
    ),
    ProcurementStrategy.BUYER_DEFINED: (
        "Custom procurement weighting configured directly by the buyer."
    ),
}


def get_strategy_weights(strategy: ProcurementStrategy) -> StrategyWeights:
    """Returns the authoritative weights for a procurement strategy."""
    return STRATEGY_REGISTRY.get(strategy, STRATEGY_REGISTRY[ProcurementStrategy.COST_OPTIMIZED])


def get_strategy_description(strategy: ProcurementStrategy) -> str:
    """Returns the human-readable description of a procurement strategy."""
    return STRATEGY_DESCRIPTIONS.get(strategy, STRATEGY_DESCRIPTIONS[ProcurementStrategy.COST_OPTIMIZED])
