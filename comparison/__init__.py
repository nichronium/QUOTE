"""
Comparison and Ranking Package for Multi-Supplier Quotation Intelligence.
Provides deterministic, dimension-safe, currency-aware quotation comparison matrices,
L1/L2/L3 quote-level rankings, and item-level split sourcing recommendations.
"""

from comparison.models import (
    ChargeAllocationMethod,
    CommercialValue,
    ComparisonIssue,
    ComparisonIssueCode,
    ComparisonIssueSeverity,
    ItemComparison,
    NormalizedItemPrice,
    RFQComparison,
    RFQDocument,
    SupplierCommercialTerms,
    SupplierComparison,
    SupplierQuoteSubmission,
)
from comparison.normalizer import CommercialNormalizer
from comparison.comparator import QuotationComparator
from comparison.ranking import (
    GlobalRankingReport,
    ItemSplitRecommendation,
    RankingEngine,
    SupplierRankingResult,
)

__all__ = [
    "ChargeAllocationMethod",
    "CommercialNormalizer",
    "CommercialValue",
    "ComparisonIssue",
    "ComparisonIssueCode",
    "ComparisonIssueSeverity",
    "GlobalRankingReport",
    "ItemComparison",
    "ItemSplitRecommendation",
    "NormalizedItemPrice",
    "QuotationComparator",
    "RankingEngine",
    "RFQComparison",
    "RFQDocument",
    "SupplierCommercialTerms",
    "SupplierComparison",
    "SupplierQuoteSubmission",
    "SupplierRankingResult",
]
