"""
Quote Intelligence CLI Runner.
Parses a supplier quotation document (PDF, Excel, or CSV) and outputs the validated CanonicalQuote JSON
along with independent financial reconciliation and validation reports.
"""

import argparse
import json
from pathlib import Path
import sys

from extraction.extractor import QuoteExtractor
from parsers.csv_parser import CSVParser
from parsers.excel_parser import ExcelParser
from parsers.pdf_parser import PDFParser


def process_document(file_path: str, include_provenance: bool = False, json_only: bool = False) -> dict:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Quotation file not found: {path}")

    suffix = path.suffix.lower()
    if suffix in [".xlsx", ".xls"]:
        parser = ExcelParser()
    elif suffix == ".pdf":
        parser = PDFParser()
    elif suffix == ".csv":
        parser = CSVParser()
    else:
        raise ValueError(f"Unsupported document format: {suffix}. Supported: .pdf, .xlsx, .xls, .csv")

    ast = parser.parse(path)
    extractor = QuoteExtractor()
    quote = extractor.extract(ast)
    validation_report = extractor.reconciler.reconcile(quote)

    quote_dict = quote.model_dump(mode="json")

    fin = validation_report.financial_model
    report_dict = {
        "status": validation_report.overall_status.value,
        "is_valid": validation_report.is_valid,
        "item_count": len(quote.items),
        "total_landed_cost": str(quote.calculate_total_landed_cost()),
        "calculated_expected_total": str(fin.calculated_expected_total),
        "supplier_stated_grand_total": str(fin.supplier_stated_grand_total) if fin.supplier_stated_grand_total is not None else None,
        "discrepancy": str(fin.discrepancy) if fin.discrepancy is not None else None,
        "reconciliation_status": fin.reconciliation_status.value,
        "overall_confidence": quote.extraction_metadata.overall_confidence,
        "warnings": quote.extraction_metadata.warnings,
    }

    result = {
        "canonical_quote": quote_dict,
        "validation_report": report_dict
    }

    if json_only:
        print(json.dumps(result, indent=2, default=str))
    else:
        print("\n=======================================================")
        print(f"  QUOTE INTELLIGENCE: {path.name}")
        print("=======================================================")
        print(f"Supplier                  : {quote.supplier_raw_name or 'N/A'}")
        print(f"Quote Number              : {quote.quote_number or 'N/A'}")
        print(f"Currency                  : {quote.currency}")
        print(f"Line Items Extracted      : {len(quote.items)}")
        print(f"Additional Charges        : {len(quote.additional_charges)}")
        print("-------------------------------------------------------")
        print(f"Calculated Expected Total : {quote.currency} {fin.calculated_expected_total:,.2f}")
        if fin.supplier_stated_grand_total is not None:
            print(f"Supplier-Stated Total     : {quote.currency} {fin.supplier_stated_grand_total:,.2f}")
            print(f"Financial Discrepancy     : {quote.currency} {fin.discrepancy:,.2f}")
        else:
            print("Supplier-Stated Total     : [Not Provided]")
        print(f"Reconciliation Status     : {fin.reconciliation_status.value}")
        print(f"Overall Confidence        : {quote.extraction_metadata.overall_confidence * 100.0:.1f}%")
        print("=======================================================\n")
        if quote.extraction_metadata.warnings:
            print("Validation Warnings:")
            for w in quote.extraction_metadata.warnings:
                print(f"  - {w}")
            print("")

    return result


def main():
    parser = argparse.ArgumentParser(description="Quote Intelligence CLI - Semantic Quotation Extractor")
    parser.add_argument("file_path", help="Path to supplier quotation file (.xlsx, .xls, .csv, .pdf)")
    parser.add_argument("--json", action="store_true", help="Output pure structured JSON payload")
    parser.add_argument("--provenance", action="store_true", help="Include full cell-level provenance")

    args = parser.parse_args()
    process_document(args.file_path, include_provenance=args.provenance, json_only=args.json)


if __name__ == "__main__":
    main()

