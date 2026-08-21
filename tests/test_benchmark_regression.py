from datetime import date
from decimal import Decimal
from pathlib import Path
import pytest

from core.canonical_quote import CanonicalQuote
from evaluation.benchmark import GroundTruthBenchmark
from extraction.extractor import QuoteExtractor
from parsers.csv_parser import CSVParser
from parsers.excel_parser import ExcelParser


@pytest.fixture
def base_paths():
    base_dir = Path(__file__).resolve().parent.parent
    return {
        "synthetic": base_dir / "datasets" / "synthetic",
        "ground_truth": base_dir / "datasets" / "ground_truth",
        "test_runs": base_dir / "test_runs"
    }


def test_benchmark_messy_realistic_quote_xlsx(base_paths):
    file_path = base_paths["test_runs"] / "TEST-0008" / "source" / "messy_realistic.xlsx"
    if not file_path.exists():
        pytest.skip("TEST-0008 source file not found")

    parser = ExcelParser()
    ast = parser.parse(file_path)
    extractor = QuoteExtractor()
    quote = extractor.extract(ast)

    assert quote.supplier_raw_name == "Shree Om Industrial Solutions"
    assert quote.quote_number == "SOIS/QUO/1198"
    assert quote.quote_date == date(2026, 8, 11)
    
    # Commercial Terms & Provenance
    assert quote.payment_terms is not None
    assert quote.payment_terms.credit_days == 30
    assert "30 Days" in quote.payment_terms.raw_text
    assert quote.payment_terms.provenance is not None

    assert quote.delivery_terms is not None
    assert quote.delivery_terms.lead_time_days_default == 7
    assert "7 Days" in quote.delivery_terms.raw_text
    assert quote.delivery_terms.provenance is not None

    # Line Items
    assert len(quote.items) == 3
    
    item0 = quote.items[0]
    assert item0.raw_description == "MS Hex Bolt M12x50"
    assert item0.quoted_qty == Decimal("250")
    assert item0.quoted_uom == "PCS"
    assert item0.unit_price == Decimal("11.75")
    assert item0.tax_rate_pct == Decimal("18")
    assert item0.provenance.cell_ref == "B7"
    assert item0.confidence_score >= 0.85

    assert quote.items[1].raw_description == "Nut M12 heavy"
    assert quote.items[1].quoted_qty == Decimal("250")
    assert quote.items[1].unit_price == Decimal("4.25")
    assert quote.items[1].provenance.cell_ref == "B8"

    assert quote.items[2].raw_description == "Flat Washer M12"
    assert quote.items[2].quoted_qty == Decimal("500")
    assert quote.items[2].unit_price == Decimal("1.85")
    assert quote.items[2].provenance.cell_ref == "B9"

    assert quote.calculate_total_landed_cost() == Decimal("5811.50")
    assert quote.extraction_metadata.overall_confidence == 1.0


def test_benchmark_flat_relational_clean_quote_xlsx(base_paths):
    file_path = base_paths["test_runs"] / "TEST-0007" / "source" / "clean_quote.xlsx"
    if not file_path.exists():
        pytest.skip("TEST-0007 source file not found")

    parser = ExcelParser()
    ast = parser.parse(file_path)
    extractor = QuoteExtractor()
    quote = extractor.extract(ast)

    assert quote.supplier_raw_name == "ABC Industrial Supplies Pvt Ltd"
    assert quote.quote_number == "Q-ABC-001"
    assert quote.quote_date == date(2026, 8, 20)
    assert len(quote.items) == 3

    assert quote.items[0].raw_description == "SKF 6205 Deep Groove Bearing"
    assert quote.items[0].provenance.cell_ref == "E2"
    assert quote.calculate_total_landed_cost() == Decimal("14219.00")
    assert quote.extraction_metadata.overall_confidence == 1.0


def test_missing_supplier_penalizes_confidence(tmp_path: Path):
    csv_file = tmp_path / "anonymous_quote.csv"
    csv_content = """Item Code,Description,Qty,UOM,Rate,Tax %
BOLT-01,Hex Bolt M8,100,PCS,5.00,18%
"""
    with open(csv_file, "w", encoding="utf-8") as f:
        f.write(csv_content)

    ast = CSVParser().parse(csv_file)
    quote = QuoteExtractor().extract(ast)

    assert quote.supplier_raw_name == "Unknown Supplier"
    assert quote.extraction_metadata.overall_confidence <= 0.70
    assert any("Supplier identity could not be verified" in w for w in quote.extraction_metadata.warnings)


def test_benchmark_01_clean_quote_xlsx(base_paths):
    excel_path = base_paths["synthetic"] / "01_clean_quote.xlsx"
    gt_path = base_paths["ground_truth"] / "01_clean_quote.json"

    parser = ExcelParser()
    ast = parser.parse(excel_path)
    extractor = QuoteExtractor()
    actual = extractor.extract(ast)

    with open(gt_path, "r", encoding="utf-8") as f:
        expected = CanonicalQuote.model_validate_json(f.read())

    benchmark = GroundTruthBenchmark()
    report = benchmark.evaluate(actual, expected, fixture_name="01_clean_quote.xlsx")
    assert report.overall_accuracy >= 95.0


def test_benchmark_02_multi_sheet_quote_xlsx(base_paths):
    excel_path = base_paths["synthetic"] / "02_multi_sheet_quote.xlsx"
    gt_path = base_paths["ground_truth"] / "02_multi_sheet_quote.json"

    parser = ExcelParser()
    ast = parser.parse(excel_path)
    extractor = QuoteExtractor()
    actual = extractor.extract(ast)

    with open(gt_path, "r", encoding="utf-8") as f:
        expected = CanonicalQuote.model_validate_json(f.read())

    benchmark = GroundTruthBenchmark()
    report = benchmark.evaluate(actual, expected, fixture_name="02_multi_sheet_quote.xlsx")
    assert "Zenith Machinery" in actual.supplier_raw_name
    assert report.overall_accuracy >= 95.0


def test_benchmark_03_multiline_desc_xlsx(base_paths):
    excel_path = base_paths["synthetic"] / "03_multiline_desc_quote.xlsx"
    gt_path = base_paths["ground_truth"] / "03_multiline_desc_quote.json"

    parser = ExcelParser()
    ast = parser.parse(excel_path)
    extractor = QuoteExtractor()
    actual = extractor.extract(ast)

    with open(gt_path, "r", encoding="utf-8") as f:
        expected = CanonicalQuote.model_validate_json(f.read())

    benchmark = GroundTruthBenchmark()
    report = benchmark.evaluate(actual, expected, fixture_name="03_multiline_desc_quote.xlsx")
    assert len(actual.items) == 2
    assert report.overall_accuracy >= 95.0


def test_benchmark_04_clean_csv(base_paths):
    csv_path = base_paths["synthetic"] / "04_clean_quote.csv"
    gt_path = base_paths["ground_truth"] / "04_clean_quote.json"

    parser = CSVParser()
    ast = parser.parse(csv_path)
    extractor = QuoteExtractor()
    actual = extractor.extract(ast)

    with open(gt_path, "r", encoding="utf-8") as f:
        expected = CanonicalQuote.model_validate_json(f.read())

    benchmark = GroundTruthBenchmark()
    report = benchmark.evaluate(actual, expected, fixture_name="04_clean_quote.csv")
    assert report.overall_accuracy == 100.0
