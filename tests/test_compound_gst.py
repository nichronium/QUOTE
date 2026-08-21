"""
Compound GST (CGST + SGST + IGST + CESS) Split Tax Regression Tests.
"""
from decimal import Decimal
import io, openpyxl
from pathlib import Path
import pytest
from core.canonical_quote import FieldStatus, TaxComponent
from extraction.extractor import QuoteExtractor
from parsers.excel_parser import ExcelParser

def _build_and_extract(builder_func, tmp_path: Path):
    wb = openpyxl.Workbook()
    builder_func(wb)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fpath = tmp_path / 'compound_gst_test.xlsx'
    fpath.write_bytes(bio.read())
    ast = ExcelParser().parse(fpath)
    extractor = QuoteExtractor()
    quote = extractor.extract(ast)

    report = extractor.reconciler.reconcile(quote)
    return quote, report

def test_cgst_sgst_9_pct_split_totals_18_pct(tmp_path):
    def build(wb):
        ws = wb.active
        ws.title = 'Quotation'
        ws.append(['Vendor: Precision Switchgear Ltd', '', '', '', ''])
        ws.append(['Quote Ref: PS-2026-901', '', '', '', ''])
        ws.append([])
        ws.append(['Description', 'Qty', 'Unit Rate', 'CGST %', 'SGST %'])
        ws.append(['Contactor 3-Pole 32A 230V', 10, 1500.0, 9.0, 9.0])
        ws.append([])
        ws.append(['Grand Total', '', '', '', 17700.0])
    quote, report = _build_and_extract(build, tmp_path)
    assert len(quote.items) == 1
    item = quote.items[0]
    assert item.tax_rate_pct == Decimal('18.0')
    assert len(item.tax_components) == 2
    types = {c.tax_type: c.rate_pct for c in item.tax_components}
    assert types['CGST'] == Decimal('9.0')
    assert types['SGST'] == Decimal('9.0')
    assert item.calculate_line_landed_cost() == Decimal('17700.00')
    assert report.financial_model.reconciliation_status.value == 'RECONCILED'
    assert report.financial_model.calculated_expected_total == Decimal('17700.00')

def test_cgst_sgst_2_point_5_pct_split_totals_5_pct(tmp_path):
    def build(wb):
        ws = wb.active
        ws.title = 'Quotation'
        ws.append(['Supplier: National Chemical Corp', '', '', '', ''])
        ws.append([])
        ws.append(['Item Name', 'Qty', 'UOM', 'Price', 'CGST %', 'SGST %'])
        ws.append(['Industrial Solvent Grade A', 100, 'LTR', 250.0, 2.5, 2.5])
        ws.append([])
        ws.append(['Grand Total', '', '', '', '', 26250.0])
    quote, report = _build_and_extract(build, tmp_path)
    assert len(quote.items) == 1
    item = quote.items[0]
    assert item.tax_rate_pct == Decimal('5.0')
    types = {c.tax_type: c.rate_pct for c in item.tax_components}
    assert types['CGST'] == Decimal('2.5')
    assert types['SGST'] == Decimal('2.5')
    assert item.calculate_line_landed_cost() == Decimal('26250.00')
    assert report.financial_model.reconciliation_status.value == 'RECONCILED'

def test_igst_single_tax_component(tmp_path):
    def build(wb):
        ws = wb.active
        ws.title = 'Quotation'
        ws.append(['Description', 'Qty', 'Rate', 'IGST %'])
        ws.append(['CNC Router Spindle 3kW', 2, 45000.0, 18.0])
    quote, report = _build_and_extract(build, tmp_path)
    assert len(quote.items) == 1
    item = quote.items[0]
    assert item.tax_rate_pct == Decimal('18.0')
    assert len(item.tax_components) == 1
    assert item.tax_components[0].tax_type == 'IGST'
    assert item.tax_components[0].rate_pct == Decimal('18.0')
    assert item.calculate_line_landed_cost() == Decimal('106200.00')

def test_split_tax_with_stated_total_gst_column_no_double_counting(tmp_path):
    def build(wb):
        ws = wb.active
        ws.title = 'Commercial_Quote'
        ws.append(['Description', 'Qty', 'Rate', 'CGST %', 'SGST %', 'Total GST %'])
        ws.append(['High Voltage Relay 24V', 50, 400.0, 9.0, 9.0, 18.0])
        ws.append([])
        ws.append(['Grand Total', '', '', '', '', 23600.0])
    quote, report = _build_and_extract(build, tmp_path)
    assert len(quote.items) == 1
    item = quote.items[0]
    assert item.tax_rate_pct == Decimal('18.0')
    assert item.calculate_line_landed_cost() == Decimal('23600.00')
    assert report.financial_model.reconciliation_status.value == 'RECONCILED'
