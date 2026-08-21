"""
Field-Level Ground Truth Benchmark Evaluator.
Compares extracted CanonicalQuote objects against Golden Ground-Truth JSON files
and reports exact field-by-field accuracy metrics.
"""

from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from rapidfuzz import fuzz

from core.canonical_quote import CanonicalQuote


@dataclass
class FieldScore:
    field_name: str
    matches: int = 0
    total: int = 0

    @property
    def percentage(self) -> float:
        return (self.matches / self.total * 100.0) if self.total > 0 else 100.0


@dataclass
class BenchmarkReport:
    fixture_name: str
    supplier_accuracy: float = 0.0
    quote_number_accuracy: float = 0.0
    date_accuracy: float = 0.0
    item_count_accuracy: float = 0.0
    description_accuracy: float = 0.0
    quantity_accuracy: float = 0.0
    uom_accuracy: float = 0.0
    unit_price_accuracy: float = 0.0
    tax_accuracy: float = 0.0
    landed_cost_accuracy: float = 0.0
    details: List[str] = field(default_factory=list)

    @property
    def overall_accuracy(self) -> float:
        metrics = [
            self.supplier_accuracy, self.quote_number_accuracy, self.date_accuracy,
            self.item_count_accuracy, self.description_accuracy, self.quantity_accuracy,
            self.uom_accuracy, self.unit_price_accuracy, self.tax_accuracy,
            self.landed_cost_accuracy
        ]
        return round(sum(metrics) / len(metrics), 1)

    def print_matrix(self):
        print(f"\n==================================================")
        print(f"EXTRACTION BENCHMARK: {self.fixture_name}")
        print(f"==================================================")
        print(f"{'Supplier:':<25} {self.supplier_accuracy:>5.1f}%")
        print(f"{'Quote Number:':<25} {self.quote_number_accuracy:>5.1f}%")
        print(f"{'Date:':<25} {self.date_accuracy:>5.1f}%")
        print(f"{'Item Count:':<25} {self.item_count_accuracy:>5.1f}%")
        print(f"{'Description (Fuzzy):':<25} {self.description_accuracy:>5.1f}%")
        print(f"{'Quantity:':<25} {self.quantity_accuracy:>5.1f}%")
        print(f"{'UOM:':<25} {self.uom_accuracy:>5.1f}%")
        print(f"{'Unit Price:':<25} {self.unit_price_accuracy:>5.1f}%")
        print(f"{'Tax Rate:':<25} {self.tax_accuracy:>5.1f}%")
        print(f"{'Landed Cost:':<25} {self.landed_cost_accuracy:>5.1f}%")
        print(f"--------------------------------------------------")
        print(f"{'OVERALL SCORE:':<25} {self.overall_accuracy:>5.1f}%")
        print(f"==================================================")


class GroundTruthBenchmark:
    """Evaluates CanonicalQuote instances against verified Golden JSON fixtures."""

    def evaluate(self, actual: CanonicalQuote, expected: CanonicalQuote, fixture_name: str = "Test") -> BenchmarkReport:
        report = BenchmarkReport(fixture_name=fixture_name)

        # 1. Supplier Name (Fuzzy token sort match >= 80%)
        sim = fuzz.token_set_ratio(actual.supplier_raw_name.lower(), expected.supplier_raw_name.lower())
        report.supplier_accuracy = 100.0 if sim >= 80 else 0.0

        # 2. Quote Number
        if expected.quote_number:
            report.quote_number_accuracy = 100.0 if actual.quote_number == expected.quote_number else 0.0
        else:
            report.quote_number_accuracy = 100.0

        # 3. Quote Date
        if expected.quote_date:
            report.date_accuracy = 100.0 if actual.quote_date == expected.quote_date else 0.0
        else:
            report.date_accuracy = 100.0

        # 4. Item Count
        exp_count = len(expected.items)
        act_count = len(actual.items)
        if exp_count > 0:
            report.item_count_accuracy = max(0.0, 100.0 - abs(act_count - exp_count) / exp_count * 100.0)
        else:
            report.item_count_accuracy = 100.0 if act_count == 0 else 0.0

        # 5. Line Item Comparison
        if exp_count > 0 and act_count > 0:
            desc_scores = []
            qty_matches = 0
            uom_matches = 0
            price_matches = 0
            tax_matches = 0

            for i in range(min(act_count, exp_count)):
                act_item = actual.items[i]
                exp_item = expected.items[i]

                # Description similarity
                desc_sim = fuzz.token_set_ratio(act_item.raw_description.lower(), exp_item.raw_description.lower())
                desc_scores.append(desc_sim)

                # Quantity exact match
                if act_item.quoted_qty == exp_item.quoted_qty:
                    qty_matches += 1

                # UOM exact match
                if act_item.quoted_uom == exp_item.quoted_uom:
                    uom_matches += 1

                # Unit Price exact Decimal match
                if act_item.unit_price == exp_item.unit_price:
                    price_matches += 1

                # Tax Rate exact match
                if act_item.tax_rate_pct == exp_item.tax_rate_pct:
                    tax_matches += 1

            report.description_accuracy = round(sum(desc_scores) / exp_count, 1)
            report.quantity_accuracy = round(qty_matches / exp_count * 100.0, 1)
            report.uom_accuracy = round(uom_matches / exp_count * 100.0, 1)
            report.unit_price_accuracy = round(price_matches / exp_count * 100.0, 1)
            report.tax_accuracy = round(tax_matches / exp_count * 100.0, 1)

        # 6. Total Landed Cost
        act_landed = actual.calculate_total_landed_cost()
        exp_landed = expected.calculate_total_landed_cost()
        if exp_landed > Decimal("0.0"):
            diff = abs(act_landed - exp_landed)
            report.landed_cost_accuracy = 100.0 if diff <= Decimal("1.00") else max(0.0, float(100.0 - (diff / exp_landed * 100.0)))
        else:
            report.landed_cost_accuracy = 100.0

        return report
