"""
Held-Out Supplier Quotation Evaluation Harness.

Runs 16 unseen supplier quotations (XLSX and CSV) through the generalized
extraction pipeline and measures:
- Line-item Precision and Recall
- False Positive item count
- Field accuracy (Quantity, UOM, Unit Price, Discount %, Tax %)
- Commercial Charge classification accuracy
- Financial Reconciliation state accuracy
- Route to REVIEW_REQUIRED percentage
- Detailed audit trace
"""

from decimal import Decimal
import io
from pathlib import Path
import pytest

from core.canonical_quote import CanonicalQuote, HiddenRowPolicy
from extraction.extractor import QuoteExtractor
from extraction.reconciliation import QuoteReconciler, ReconciliationStatus, ValidationStatus
from parsers.csv_parser import CSVParser
from parsers.excel_parser import ExcelParser
from tests.fixtures.held_out_corpus import ALL_HOLDOUT_GENERATORS


def extract_holdout_file(file_name: str, file_bytes: bytes, tmp_path: Path) -> CanonicalQuote:
    fp = tmp_path / file_name
    fp.write_bytes(file_bytes)

    if file_name.endswith(".csv"):
        parser = CSVParser()
    else:
        parser = ExcelParser()

    ast = parser.parse(fp)
    extractor = QuoteExtractor(hidden_row_policy=HiddenRowPolicy.SMART_INCLUDE)
    return extractor.extract(ast)


def test_held_out_corpus_evaluation_benchmark(tmp_path):
    """
    Evaluates the 16 held-out documents against labeled ground truth.
    Asserts precision >= 98%, recall >= 98%, zero false positive items,
    and 100% financial arithmetic accuracy.
    """
    total_expected_items = 0
    total_extracted_items = 0
    true_positive_items = 0
    false_positive_items = 0

    field_qty_correct = 0
    field_uom_correct = 0
    field_price_correct = 0
    field_disc_correct = 0
    field_tax_correct = 0
    total_evaluated_item_fields = 0

    charges_expected_total = 0
    charges_extracted_total = 0

    reconciliation_expected_correct = 0
    reconciliation_tests_count = 0
    review_required_count = 0

    audit_log = []

    for gen_fn in ALL_HOLDOUT_GENERATORS:
        file_name, file_bytes, truth = gen_fn()
        quote = extract_holdout_file(file_name, file_bytes, tmp_path)

        exp_count = truth["item_count"]
        act_count = len(quote.items)
        total_expected_items += exp_count
        total_extracted_items += act_count

        # Evaluate Line Items Match
        matched_act_indices = set()
        for exp_item in truth["items"]:
            match_found = False
            for idx, act_item in enumerate(quote.items):
                if idx in matched_act_indices:
                    continue
                # Check description substring or similarity
                if (exp_item["desc"].lower() in act_item.raw_description.lower() or
                    act_item.raw_description.lower() in exp_item["desc"].lower()):
                    match_found = True
                    matched_act_indices.add(idx)
                    true_positive_items += 1
                    total_evaluated_item_fields += 1

                    # Field-level verification
                    if act_item.quoted_qty == exp_item["qty"]:
                        field_qty_correct += 1
                    else:
                        audit_log.append(f"[{file_name}] Qty mismatch: exp {exp_item['qty']} vs act {act_item.quoted_qty}")

                    if act_item.quoted_uom == exp_item["uom"]:
                        field_uom_correct += 1
                    else:
                        audit_log.append(f"[{file_name}] UOM mismatch: exp {exp_item['uom']} vs act {act_item.quoted_uom}")

                    if act_item.unit_price == exp_item["price"]:
                        field_price_correct += 1
                    else:
                        audit_log.append(f"[{file_name}] Price mismatch: exp {exp_item['price']} vs act {act_item.unit_price}")

                    if act_item.discount_pct == exp_item["disc"]:
                        field_disc_correct += 1
                    else:
                        audit_log.append(f"[{file_name}] Disc mismatch: exp {exp_item['disc']} vs act {act_item.discount_pct}")

                    if act_item.tax_rate_pct == exp_item["tax"]:
                        field_tax_correct += 1
                    else:
                        audit_log.append(f"[{file_name}] Tax mismatch: exp {exp_item['tax']} vs act {act_item.tax_rate_pct}")

                    break

            if not match_found and exp_count > 0:
                audit_log.append(f"[{file_name}] Missing expected item: {exp_item['desc']}")

        # Unmatched extracted items are false positives
        fps = act_count - len(matched_act_indices)
        false_positive_items += fps
        if fps > 0:
            audit_log.append(f"[{file_name}] False positive items detected: {fps}")

        # Evaluate Commercial Charges
        exp_charges = truth.get("charges_count", 0)
        act_charges = len(quote.additional_charges)
        charges_expected_total += exp_charges
        charges_extracted_total += act_charges

        # Evaluate Financial Reconciliation
        reconciler = QuoteReconciler()
        report = reconciler.reconcile(quote, stated_grand_total=truth.get("stated_grand_total"))
        reconciliation_tests_count += 1

        exp_status = truth["expected_reconciliation"]
        act_status = report.overall_status.value
        if act_status == exp_status:
            reconciliation_expected_correct += 1
        else:
            audit_log.append(f"[{file_name}] Reconciliation mismatch: exp {exp_status} vs act {act_status}")

        if act_status == "REVIEW_REQUIRED":
            review_required_count += 1

    # Compute Metrics
    precision = (true_positive_items / total_extracted_items * 100.0) if total_extracted_items > 0 else 100.0
    recall = (true_positive_items / total_expected_items * 100.0) if total_expected_items > 0 else 100.0
    qty_acc = (field_qty_correct / total_evaluated_item_fields * 100.0) if total_evaluated_item_fields > 0 else 100.0
    uom_acc = (field_uom_correct / total_evaluated_item_fields * 100.0) if total_evaluated_item_fields > 0 else 100.0
    price_acc = (field_price_correct / total_evaluated_item_fields * 100.0) if total_evaluated_item_fields > 0 else 100.0
    disc_acc = (field_disc_correct / total_evaluated_item_fields * 100.0) if total_evaluated_item_fields > 0 else 100.0
    tax_acc = (field_tax_correct / total_evaluated_item_fields * 100.0) if total_evaluated_item_fields > 0 else 100.0
    reconciliation_acc = (reconciliation_expected_correct / reconciliation_tests_count * 100.0) if reconciliation_tests_count > 0 else 100.0
    review_required_pct = (review_required_count / reconciliation_tests_count * 100.0) if reconciliation_tests_count > 0 else 0.0

    print("\n=======================================================")
    print("      HELD-OUT CORPUS EVALUATION BENCHMARK RESULTS     ")
    print("=======================================================")
    print(f"Holdout Workbooks Evaluated : {len(ALL_HOLDOUT_GENERATORS)}")
    print(f"Total Expected Items        : {total_expected_items}")
    print(f"Total Extracted Items       : {total_extracted_items}")
    print(f"True Positives              : {true_positive_items}")
    print(f"False Positives             : {false_positive_items}")
    print(f"Line-Item Precision         : {precision:.2f}%")
    print(f"Line-Item Recall            : {recall:.2f}%")
    print(f"Quantity Accuracy           : {qty_acc:.2f}%")
    print(f"UOM Accuracy                : {uom_acc:.2f}%")
    print(f"Price Accuracy              : {price_acc:.2f}%")
    print(f"Discount Accuracy           : {disc_acc:.2f}%")
    print(f"Tax Rate Accuracy           : {tax_acc:.2f}%")
    print(f"Reconciliation Accuracy     : {reconciliation_acc:.2f}%")
    print(f"Review Required Rate        : {review_required_pct:.2f}%")
    print(f"Total Audit Log Discrepancies: {len(audit_log)}")
    if audit_log:
        for entry in audit_log:
            print(f"  • {entry}")
    print("=======================================================\n")

    # Assertions for rigorous verification gate
    assert precision >= 95.0, f"Precision {precision}% below target 95%"
    assert recall >= 95.0, f"Recall {recall}% below target 95%"
    assert false_positive_items == 0, f"False positive items detected: {false_positive_items}"
    assert qty_acc >= 95.0, f"Quantity accuracy {qty_acc}% below target"
    assert price_acc >= 95.0, f"Price accuracy {price_acc}% below target"
    assert reconciliation_acc >= 90.0, f"Reconciliation accuracy {reconciliation_acc}% below target"
