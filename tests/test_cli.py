"""
CLIParity & Subprocess Integration Tests.
Verifies that python -m extraction.cli executes the full extraction pipeline,
produces structured JSON output, and outputs validation reports with reconciliation status.
"""

import json
import openpyxl
from pathlib import Path
import subprocess
import sys


def test_cli_subprocess_json_output(tmp_path):
    wb = openpyxl.Workbook()

    ws = wb.active
    ws.append(['Supplier: Alpha Tools Ltd', '', '', ''])
    ws.append(['Description', 'Qty', 'Unit Price', 'GST %'])
    ws.append(['Carbide Endmill 10mm', 5, 850.0, 18.0])
    ws.append([])
    ws.append(['Grand Total', '', '', 5015.0])

    file_path = tmp_path / 'cli_test_quote.xlsx'
    wb.save(file_path)

    cmd = [sys.executable, '-m', 'extraction.cli', str(file_path), '--json']
    result = subprocess.run(cmd, capture_output=True, text=True)

    assert result.returncode == 0, f"CLY failed: {result.stderr}"
    data = json.loads(result.stdout)

    assert 'canonical_quote' in data
    assert 'validation_report' in data

    quote = data['canonical_quote']
    report = data['validation_report']

    assert len(quote['items']) == 1
    assert report['reconciliation_status'] == 'RECONCILED'
    assert report['item_count'] == 1



def test_cli_subprocess_standard_text_output(tmp_path):
    wb = openpyxl.Workbook()

    ws = wb.active
    ws.append(['Description', 'Qty', 'Unit Price', 'GST %'])
    ws.append(['Steel Flange DN50', 12, 300.0, 18.0])

    file_path = tmp_path / 'cli_text_quote.xlsx'
    wb.save(file_path)

    cmd = [sys.executable, '-m', 'extraction.cli', str(file_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)

    assert result.returncode == 0
    assert 'QUOTE INTELLIGENCE' in result.stdout
    assert 'Line Items Extracted' in result.stdout
    assert 'Reconciliation Status' in result.stdout
