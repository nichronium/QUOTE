"""
Golden Workbooks Fixture Generator.

Provides programmatically generated Excel workbooks and their exact
expected CanonicalQuote semantic ground truth for regression testing.
"""

from decimal import Decimal
import io
import openpyxl
from typing import Any, Dict, Tuple


def create_golden_clean_quote() -> Tuple[bytes, Dict[str, Any]]:
    """Standard single-sheet quotation with clean tabular layout."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quote"
    ws.append(["ABC Industrial Supplies Pvt Ltd"])
    ws.append(["Quote No: Q-ABC-001", "Date: 2026-08-20"])
    ws.append([])
    ws.append(["Part No", "Description", "Qty", "UOM", "Unit Price", "GST %"])
    ws.append(["BOLT-M12", "Hex Bolt M12x50 Grade 8.8", 100, "PCS", 15.0, 18])
    ws.append(["NUT-M12", "Hex Nut M12 Heavy Duty", 100, "PCS", 5.0, 18])
    ws.append(["WASH-M12", "Flat Washer M12 SS304", 200, "PCS", 2.5, 18])

    buf = io.BytesIO()
    wb.save(buf)

    ground_truth = {
        "supplier_raw_name": "ABC Industrial Supplies Pvt Ltd",
        "quote_number": "Q-ABC-001",
        "items_count": 3,
        "items": [
            {"sku": "BOLT-M12", "qty": Decimal("100"), "price": Decimal("15.00"), "landed": Decimal("1770.00")},
            {"sku": "NUT-M12", "qty": Decimal("100"), "price": Decimal("5.00"), "landed": Decimal("590.00")},
            {"sku": "WASH-M12", "qty": Decimal("200"), "price": Decimal("2.50"), "landed": Decimal("590.00")},
        ],
        "total_landed_cost": Decimal("2950.00")
    }
    return buf.getvalue(), ground_truth


def create_golden_tiered_quote() -> Tuple[bytes, Dict[str, Any]]:
    """Quote with line items and a separate volume pricing tier table."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Offer"
    ws.append(["Supplier: Apex Cables & Conductors"])
    ws.append(["Quote Ref: ACC/2026/89"])
    ws.append([])
    ws.append(["Item Code", "Description", "Qty", "UOM", "Rate", "Tax %"])
    ws.append(["CAB-4SQ", "Copper Armoured Cable 4 Sq.mm", 500, "MTR", 95.0, 18])
    ws.append([])
    ws.append(["VOLUME PRICING TIERS"])
    ws.append(["Min Qty", "Max Qty", "Unit Price"])
    ws.append([1, 99, 95.0])
    ws.append([100, 499, 90.0])
    ws.append([500, "", 85.0])

    buf = io.BytesIO()
    wb.save(buf)

    ground_truth = {
        "supplier_raw_name": "Apex Cables & Conductors",
        "quote_number": "ACC/2026/89",
        "items_count": 1,
        "price_tiers_count": 3,
        "quoted_landed": Decimal("56050.00"),  # 500 * 95 * 1.18
        "tier_evaluated_landed": Decimal("50150.00")  # 500 * 85 * 1.18
    }
    return buf.getvalue(), ground_truth
