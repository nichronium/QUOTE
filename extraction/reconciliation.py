"""
Validation & Independent Financial Reconciliation Engine.
Reconstructs financial expectations independently from extracted components
(line net totals, line taxes, quote-level charges, quote-level discounts)
and reconciles them against document supplier-stated totals.
Prevents circular assignments and strictly bounds confidence when financial errors occur.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from core.canonical_quote import CanonicalQuote, QuoteItem
from extraction.financial_engine import FinancialEngine, quantize_currency
from extraction.semantic_registry import STANDARD_UOM_SET


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ValidationStatus(str, Enum):
    RECONCILED = "RECONCILED"
    DISCREPANCY = "DISCREPANCY"
    CALCULATED_ONLY = "CALCULATED_ONLY"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ReconciliationStatus(str, Enum):
    RECONCILED = "RECONCILED"
    DISCREPANCY = "DISCREPANCY"
    CALCULATED_ONLY = "CALCULATED_ONLY"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass
class IndependentFinancialModel:
    """Independently calculated financial breakdown."""
    line_items_gross: Decimal = Decimal("0.0")
    line_items_discount: Decimal = Decimal("0.0")
    line_items_net: Decimal = Decimal("0.0")
    line_items_tax: Decimal = Decimal("0.0")
    line_items_landed: Decimal = Decimal("0.0")
    charges_net: Decimal = Decimal("0.0")
    charges_tax: Decimal = Decimal("0.0")
    charges_landed: Decimal = Decimal("0.0")
    calculated_expected_total: Decimal = Decimal("0.0")
    supplier_stated_subtotal: Optional[Decimal] = None
    supplier_stated_tax: Optional[Decimal] = None
    supplier_stated_grand_total: Optional[Decimal] = None
    discrepancy: Optional[Decimal] = None
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.CALCULATED_ONLY


@dataclass
class ValidationIssue:
    field_path: str
    message: str
    severity: Severity = Severity.MEDIUM
    actual_value: Optional[Any] = None
    expected_value: Optional[Any] = None


@dataclass
class ValidationReport:
    is_valid: bool = True
    overall_status: ValidationStatus = ValidationStatus.CALCULATED_ONLY
    calibrated_confidence: float = 1.0
    metadata_confidence: float = 1.0
    line_item_confidence: float = 1.0
    financial_confidence: float = 1.0
    financial_model: IndependentFinancialModel = field(default_factory=IndependentFinancialModel)
    issues: List[ValidationIssue] = field(default_factory=list)
    confidence_reasons: List[str] = field(default_factory=list)  # Human-readable confidence explanation

    @property
    def warning_messages(self) -> List[str]:
        return [f"[{issue.severity}] {issue.field_path}: {issue.message}" for issue in self.issues]

    @property
    def total_landed_cost(self) -> Decimal:
        return self.financial_model.calculated_expected_total


class QuoteReconciler:
    """
    Validates Quote schema, item metrics, and performs independent financial reconstruction.
    """

    INVALID_SUPPLIERS = {
        "unknown supplier", "unknown", "supplier", "vendor", "clean quote", "null", "none"
    }

    def reconcile(
        self,
        quote: CanonicalQuote,
        stated_grand_total: Optional[Decimal] = None,
        stated_subtotal: Optional[Decimal] = None,
        stated_tax: Optional[Decimal] = None
    ) -> ValidationReport:

        report = ValidationReport()
        model = IndependentFinancialModel()

        if stated_grand_total is None and quote.stated_grand_total_evidence and quote.stated_grand_total_evidence.normalized_value is not None:
            try:
                stated_grand_total = Decimal(str(quote.stated_grand_total_evidence.normalized_value))
            except Exception:
                pass

        # 1. Independent Financial Aggregation

        for item in quote.items:
            gross = item.gross_amount
            disc = item.discount_amount
            taxable = item.taxable_amount
            tax = item.tax_amount
            landed = item.calculate_line_landed_cost()

            model.line_items_gross += gross
            model.line_items_discount += disc
            model.line_items_net += taxable
            model.line_items_tax += tax
            model.line_items_landed += landed

        for charge in quote.additional_charges:
            model.charges_net += charge.amount
            model.charges_tax += (charge.total_with_tax - charge.amount)
            model.charges_landed += charge.total_with_tax

        model.calculated_expected_total = quantize_currency(
            (model.line_items_landed + model.charges_landed) * quote.exchange_rate_to_base
        )
        model.supplier_stated_subtotal = stated_subtotal
        model.supplier_stated_tax = stated_tax
        model.supplier_stated_grand_total = stated_grand_total

        # 2. Supplier Stated Grand Total Comparison
        if stated_grand_total is not None and stated_grand_total > Decimal("0.0"):
            discrepancy = abs(model.calculated_expected_total - stated_grand_total)
            model.discrepancy = discrepancy

            if discrepancy <= Decimal("1.00"):
                model.reconciliation_status = ReconciliationStatus.RECONCILED
            else:
                model.reconciliation_status = ReconciliationStatus.DISCREPANCY
                report.issues.append(ValidationIssue(
                    field_path="grand_total",
                    message=f"Financial discrepancy: calculated expected total ({model.calculated_expected_total}) != document stated total ({stated_grand_total}). Discrepancy: {discrepancy}",
                    severity=Severity.HIGH,
                    actual_value=str(model.calculated_expected_total),
                    expected_value=str(stated_grand_total)
                ))
        else:
            model.reconciliation_status = ReconciliationStatus.CALCULATED_ONLY
            model.discrepancy = None

        report.financial_model = model

        # 3. Supplier Name Validation
        if not quote.supplier_raw_name or quote.supplier_raw_name.lower() in self.INVALID_SUPPLIERS:
            report.issues.append(ValidationIssue(
                field_path="supplier_raw_name",
                message="Supplier identity could not be verified from document evidence.",
                severity=Severity.HIGH,
                actual_value=quote.supplier_raw_name
            ))

        # 4. Quote Number Validation
        if not quote.quote_number:
            report.issues.append(ValidationIssue(
                field_path="quote_number",
                message="Quote reference number was not found in document.",
                severity=Severity.LOW
            ))

        # 5. Line Items Validation
        if not quote.items:
            report.issues.append(ValidationIssue(
                field_path="items",
                message="No valid line items were extracted from the document.",
                severity=Severity.CRITICAL
            ))
        else:
            for item in quote.items:
                self._validate_item(item, report)

        # 6. Granular Confidence Calculations
        meta_penalties = sum(0.25 for i in report.issues if i.field_path.startswith("supplier") or i.field_path.startswith("quote_"))
        item_penalties = sum(0.15 for i in report.issues if i.field_path.startswith("items"))
        fin_penalties = 0.40 if model.reconciliation_status == ReconciliationStatus.DISCREPANCY else 0.0

        report.metadata_confidence = max(0.10, round(1.0 - meta_penalties, 2))
        report.line_item_confidence = max(0.10, round(1.0 - item_penalties, 2))
        report.financial_confidence = max(0.10, round(1.0 - fin_penalties, 2))

        # Overall Confidence Aggregation with human-readable reasons
        total_penalties = 0.0
        has_critical = False
        has_high = False
        reasons: List[str] = []

        for issue in report.issues:
            if issue.severity == Severity.CRITICAL:
                total_penalties += 0.50
                has_critical = True
                reasons.append(f"CRITICAL: {issue.field_path} — {issue.message}")
            elif issue.severity == Severity.HIGH:
                total_penalties += 0.25
                has_high = True
                reasons.append(f"HIGH: {issue.field_path} — {issue.message}")
            elif issue.severity == Severity.MEDIUM:
                total_penalties += 0.10
                reasons.append(f"MEDIUM: {issue.field_path} — {issue.message}")
            elif issue.severity == Severity.LOW:
                total_penalties += 0.05
                reasons.append(f"LOW: {issue.field_path} — {issue.message}")

        if model.reconciliation_status == ReconciliationStatus.DISCREPANCY:
            total_penalties += 0.25
            has_high = True
            reasons.append("DISCREPANCY: Calculated total does not match supplier-stated total.")

        if model.reconciliation_status == ReconciliationStatus.RECONCILED:
            reasons.append("RECONCILED: Calculated total matches supplier-stated total within tolerance.")
        elif model.reconciliation_status == ReconciliationStatus.CALCULATED_ONLY:
            reasons.append("CALCULATED_ONLY: Supplier-stated total not found in document; arithmetic verified internally.")

        final_conf = max(0.10, min(1.0, 1.0 - total_penalties))

        # Deterministic caps by severity
        if has_critical:
            final_conf = min(0.30, final_conf)   # CRITICAL: hard cap at 30%
            reasons.append("Confidence capped at 30% due to CRITICAL extraction failure.")
        elif model.reconciliation_status == ReconciliationStatus.DISCREPANCY:
            final_conf = min(0.55, final_conf)   # DISCREPANCY: cap at 55%
            reasons.append("Confidence capped at 55% due to financial discrepancy with supplier total.")
        elif has_high:
            final_conf = min(0.65, final_conf)   # HIGH issue: cap at 65%
            reasons.append("Confidence capped at 65% due to HIGH severity validation issue.")

        report.calibrated_confidence = round(final_conf, 2)
        report.confidence_reasons = reasons

        # Determine Final Document Status across the 5 standard states
        if has_critical or not quote.items:
            report.overall_status = ValidationStatus.INSUFFICIENT_DATA
            report.is_valid = False
        elif model.reconciliation_status == ReconciliationStatus.DISCREPANCY:
            report.overall_status = ValidationStatus.DISCREPANCY
            report.is_valid = False
        elif has_high or any(i.severity == Severity.HIGH for i in report.issues):
            report.overall_status = ValidationStatus.REVIEW_REQUIRED
            report.is_valid = False
        elif any(i.severity == Severity.MEDIUM for i in report.issues):
            report.overall_status = ValidationStatus.REVIEW_REQUIRED
            report.is_valid = True
        elif model.reconciliation_status == ReconciliationStatus.RECONCILED:
            report.overall_status = ValidationStatus.RECONCILED
            report.is_valid = True
        elif model.reconciliation_status == ReconciliationStatus.CALCULATED_ONLY:
            report.overall_status = ValidationStatus.CALCULATED_ONLY
            report.is_valid = True
        else:
            report.overall_status = ValidationStatus.CALCULATED_ONLY
            report.is_valid = True

        return report


    def _validate_item(self, item: QuoteItem, report: ValidationReport):
        prefix = f"items[{item.line_index}]"

        if item.unit_price is None or item.unit_price <= Decimal("0.0"):
            report.issues.append(ValidationIssue(
                field_path=f"{prefix}.unit_price",
                message=f"Unit price is zero or missing for '{item.raw_description}'.",
                severity=Severity.HIGH,
                actual_value=str(item.unit_price) if item.unit_price is not None else None
            ))

        if item.quoted_qty is None or item.quoted_qty <= Decimal("0.0"):
            report.issues.append(ValidationIssue(
                field_path=f"{prefix}.quoted_qty",
                message="Quoted quantity is missing or zero.",
                severity=Severity.HIGH,
                actual_value=str(item.quoted_qty) if item.quoted_qty is not None else None
            ))

        if item.quoted_uom:
            clean_uom = item.quoted_uom.upper().strip()
            if clean_uom not in STANDARD_UOM_SET:
                report.issues.append(ValidationIssue(
                    field_path=f"{prefix}.quoted_uom",
                    message=f"Unrecognized or non-standard UOM '{item.quoted_uom}'.",
                    severity=Severity.MEDIUM,
                    actual_value=item.quoted_uom
                ))
        else:
            report.issues.append(ValidationIssue(
                field_path=f"{prefix}.quoted_uom",
                message="UOM not specified in document.",
                severity=Severity.LOW,
                actual_value=None
            ))

        if len(item.raw_description.strip()) < 2:
            report.issues.append(ValidationIssue(
                field_path=f"{prefix}.raw_description",
                message="Description is suspiciously short.",
                severity=Severity.MEDIUM,
                actual_value=item.raw_description
            ))
