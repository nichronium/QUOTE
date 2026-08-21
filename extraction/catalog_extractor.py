"""
Dedicated Item Master Catalog Extractor.
Parses DocumentAST representations into canonical ItemMasterRecord catalog entries.
Specifically designed for company product catalogs (does NOT require supplier quote semantics like prices, taxes, or freight).
"""

from dataclasses import dataclass, field
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from matching.models import ItemMasterRecord
from parsers.base import DocumentAST, ExtractedTable, TableRow


@dataclass
class CatalogExtractionResult:
    """Result of catalog table extraction and structure detection."""
    items: List[ItemMasterRecord] = field(default_factory=list)
    raw_preview_data: List[Dict[str, Any]] = field(default_factory=list)
    detected_sheet: str = "Primary Sheet"
    detected_columns: Dict[str, str] = field(default_factory=dict)
    total_rows_scanned: int = 0
    valid_products_count: int = 0
    warnings: List[str] = field(default_factory=list)
    confidence_score: float = 0.0
    confidence_explanation: str = ""
    is_valid_catalog: bool = False
    rejection_reason: Optional[str] = None


class CatalogExtractor:
    """Enterprise Catalog & Product Table Extractor."""

    SKU_ALIASES = [
        "internal sku", "company sku", "our sku", "product code", "product_code",
        "item code", "item_code", "material code", "mat code", "part number", "part_number",
        "part no", "part_no", "item no", "item_no", "catalog no", "catalog number", "sku", "code"
    ]

    DESC_ALIASES = [
        "canonical description", "product description", "product_description",
        "item description", "item_description", "product name", "product_name",
        "item name", "item_name", "material description", "description", "details",
        "specifications", "specification", "item", "product", "particulars"
    ]

    MPN_ALIASES = [
        "manufacturer part number", "manufacturer_part_number", "manufacturer pn",
        "manufacturer_pn", "mfr part no", "mfr part number", "mfr pn", "mfr_pn",
        "mfg part no", "mfg pn", "mpn", "oem part number", "oem pn", "catalog no", "cat no"
    ]

    UOM_ALIASES = [
        "stocking uom", "stocking_uom", "stocking unit", "stock unit", "base uom",
        "base unit", "unit of measure", "unit", "units", "uom", "u.o.m", "packaging unit",
        "pack size", "measure"
    ]

    BRAND_ALIASES = [
        "brand", "brand name", "manufacturer", "manufacturer name", "make", "oem",
        "vendor", "supplier", "origin"
    ]

    SPEC_ALIASES = [
        "specifications", "spec", "specs", "technical specifications", "specification",
        "material", "rating", "dimensions", "size", "grade", "voltage", "type",
        "category", "group", "family"
    ]

    def extract(self, ast: DocumentAST) -> CatalogExtractionResult:
        """
        Extracts product catalog records from a DocumentAST.
        """
        candidate_tables: List[Tuple[ExtractedTable, float, Dict[str, int], str]] = []

        # 1. Collect all candidate tables from pages and AST root
        all_tables: List[Tuple[ExtractedTable, str]] = []
        for t in ast.tables:
            all_tables.append((t, t.sheet_name or "Sheet1"))
        for page in ast.pages:
            for t in page.tables:
                all_tables.append((t, page.sheet_name or f"Page {page.page_number}"))

        if not all_tables and ast.grid:
            # Grid fallback if parser built grid without pre-segmented tables
            pass

        if not all_tables:
            return CatalogExtractionResult(
                is_valid_catalog=False,
                rejection_reason="No tabular data could be identified in the uploaded document.",
                confidence_score=0.0,
                confidence_explanation="Zero tables detected in document AST."
            )

        # 2. Score each candidate table specifically for catalog semantics
        for table, sheet_name in all_tables:
            score, col_map = self._evaluate_catalog_table(table)
            if score > 0.3:
                candidate_tables.append((table, score, col_map, sheet_name))

        if not candidate_tables:
            return CatalogExtractionResult(
                is_valid_catalog=False,
                rejection_reason="Document contains tables, but none match a product catalog structure (missing SKU and Description columns).",
                confidence_score=0.1,
                confidence_explanation="Tables detected lack catalog identifier or description headers."
            )

        # Pick highest scoring candidate table
        candidate_tables.sort(key=lambda x: (x[1], len(x[0].rows)), reverse=True)
        best_table, table_score, col_map, sheet_name = candidate_tables[0]

        # 3. Extract items from best table rows
        extracted_records: List[ItemMasterRecord] = []
        preview_data: List[Dict[str, Any]] = []
        warnings: List[str] = []
        seen_skus: Set[str] = set()

        sku_col = col_map.get("sku")
        desc_col = col_map.get("desc")
        mpn_col = col_map.get("mpn")
        uom_col = col_map.get("uom")
        brand_col = col_map.get("brand")
        spec_cols = col_map.get("specs", [])

        # Build detected columns label map for UI display
        headers = best_table.headers
        detected_cols_display: Dict[str, str] = {}
        if sku_col is not None and sku_col < len(headers):
            detected_cols_display["Internal SKU"] = f"Column {sku_col+1} ({headers[sku_col] or 'SKU'})"
        if desc_col is not None and desc_col < len(headers):
            detected_cols_display["Description"] = f"Column {desc_col+1} ({headers[desc_col] or 'Description'})"
        if mpn_col is not None and mpn_col < len(headers):
            detected_cols_display["Manufacturer PN"] = f"Column {mpn_col+1} ({headers[mpn_col] or 'MPN'})"
        if uom_col is not None and uom_col < len(headers):
            detected_cols_display["Stocking UOM"] = f"Column {uom_col+1} ({headers[uom_col] or 'UOM'})"
        if brand_col is not None and brand_col < len(headers):
            detected_cols_display["Brand"] = f"Column {brand_col+1} ({headers[brand_col] or 'Brand'})"

        row_count = 0
        for r_idx, row in enumerate(best_table.rows):
            cells = row.cells if row.cells else [c.normalized_str for c in row.cell_objects]
            if not cells or not any(str(c).strip() for c in cells):
                continue
            
            # Skip if row is identical to header row
            if [str(c).lower().strip() for c in cells] == [str(h).lower().strip() for h in headers]:
                continue

            row_count += 1
            raw_sku = str(cells[sku_col]).strip() if sku_col is not None and sku_col < len(cells) and cells[sku_col] is not None else ""
            raw_desc = str(cells[desc_col]).strip() if desc_col is not None and desc_col < len(cells) and cells[desc_col] is not None else ""
            raw_mpn = str(cells[mpn_col]).strip() if mpn_col is not None and mpn_col < len(cells) and cells[mpn_col] is not None else ""
            raw_uom = str(cells[uom_col]).strip() if uom_col is not None and uom_col < len(cells) and cells[uom_col] is not None else ""
            raw_brand = str(cells[brand_col]).strip() if brand_col is not None and brand_col < len(cells) and cells[brand_col] is not None else ""

            # Check if row is purely whitespace or summary
            if not raw_sku and not raw_desc:
                continue

            # Fallback & Validation for SKU
            if not raw_sku or raw_sku.lower() == "none":
                if raw_mpn and raw_mpn.lower() != "none":
                    final_sku = raw_mpn.upper()
                else:
                    final_sku = f"SKU-{len(extracted_records)+1:03d}"
                    warnings.append(f"Row {r_idx+1}: Missing SKU; generated placeholder '{final_sku}' from description.")
            else:
                final_sku = raw_sku.upper()

            # Check for duplicate SKU in this file
            if final_sku in seen_skus:
                warnings.append(f"Row {r_idx+1}: Duplicate SKU '{final_sku}' detected.")
            seen_skus.add(final_sku)

            # Fallback & Validation for Description
            if not raw_desc or raw_desc.lower() == "none":
                final_desc = final_sku
                warnings.append(f"Row {r_idx+1}: Missing description for SKU '{final_sku}'; defaulted to SKU identifier.")
            else:
                final_desc = raw_desc

            # Fallback & Validation for UOM
            final_uom = self._normalize_uom(raw_uom)
            if not raw_uom or raw_uom.lower() == "none":
                warnings.append(f"Row {r_idx+1}: Missing stocking unit for SKU '{final_sku}'; defaulted to 'PCS'.")

            # Collect specifications from extra mapped spec columns
            specs = {}
            for col_i in spec_cols:
                if col_i < len(cells) and col_i < len(headers) and cells[col_i]:
                    header_name = headers[col_i].strip() or f"Spec {col_i+1}"
                    specs[header_name] = str(cells[col_i]).strip()

            item_id = f"ITEM-{len(extracted_records)+1:03d}"
            item_record = ItemMasterRecord(
                internal_item_id=item_id,
                internal_sku=final_sku,
                manufacturer_part_number=raw_mpn if raw_mpn and raw_mpn.lower() != "none" else None,
                approved_supplier_part_numbers=[final_sku] if final_sku else [],
                canonical_description=final_desc,
                stocking_uom=final_uom,
                brand=raw_brand if raw_brand and raw_brand.lower() != "none" else "Standard",
                specifications=specs
            )
            extracted_records.append(item_record)

            preview_data.append({
                "internal_sku": final_sku,
                "canonical_description": final_desc,
                "manufacturer_part_number": raw_mpn if raw_mpn and raw_mpn.lower() != "none" else "",
                "stocking_uom": final_uom,
                "brand": raw_brand if raw_brand and raw_brand.lower() != "none" else "Standard",
                "specifications": specs,
                "approved_supplier_part_numbers": [final_sku]
            })

        if not extracted_records:
            return CatalogExtractionResult(
                is_valid_catalog=False,
                rejection_reason="No valid product rows could be extracted from the candidate catalog table.",
                confidence_score=0.1,
                confidence_explanation="Table header was identified but no data rows had valid product content."
            )

        # Confidence calculation
        confidence = min(1.0, round(table_score, 2))
        if warnings:
            deduction = min(0.20, len(warnings) * 0.02)
            confidence = max(0.60, round(confidence - deduction, 2))

        explanation = f"Detected catalog table in '{sheet_name}' with {len(extracted_records)} product(s)."
        if warnings:
            explanation += f" ({len(warnings)} quality warning(s) noted)."

        return CatalogExtractionResult(
            items=extracted_records,
            raw_preview_data=preview_data,
            detected_sheet=sheet_name,
            detected_columns=detected_cols_display,
            total_rows_scanned=row_count,
            valid_products_count=len(extracted_records),
            warnings=warnings[:10],
            confidence_score=confidence,
            confidence_explanation=explanation,
            is_valid_catalog=True,
            rejection_reason=None
        )

    def _evaluate_catalog_table(self, table: ExtractedTable) -> Tuple[float, Dict[str, Any]]:
        """Evaluates table headers and column structures for product catalog semantics."""
        headers = [str(h).lower().strip() for h in table.headers]
        if not headers:
            # Check first row
            if table.rows:
                first_cells = table.rows[0].cells if table.rows[0].cells else [c.normalized_str for c in table.rows[0].cell_objects]
                headers = [str(c).lower().strip() for c in first_cells]

        col_map: Dict[str, Any] = {"specs": []}
        score = 0.0

        for idx, h in enumerate(headers):
            h_clean = re.sub(r"[^\w\s]", "", h).strip()
            
            # SKU check
            if any(alias in h_clean for alias in self.SKU_ALIASES) and "sku" not in col_map:
                col_map["sku"] = idx
                score += 0.35
            # Description check
            elif any(alias in h_clean for alias in self.DESC_ALIASES) and "desc" not in col_map:
                col_map["desc"] = idx
                score += 0.35
            # MPN check
            elif any(alias in h_clean for alias in self.MPN_ALIASES) and "mpn" not in col_map:
                col_map["mpn"] = idx
                score += 0.15
            # UOM check
            elif any(alias in h_clean for alias in self.UOM_ALIASES) and "uom" not in col_map:
                col_map["uom"] = idx
                score += 0.15
            # Brand check
            elif any(alias in h_clean for alias in self.BRAND_ALIASES) and "brand" not in col_map:
                col_map["brand"] = idx
                score += 0.10
            # Specs check
            elif any(alias in h_clean for alias in self.SPEC_ALIASES):
                col_map["specs"].append(idx)
                score += 0.05

        # Fallbacks for positional table structures without explicit matching header names
        if "sku" not in col_map and len(headers) >= 1:
            # If col 0 looks like codes
            col_map["sku"] = 0
            score += 0.20
        if "desc" not in col_map and len(headers) >= 2:
            col_map["desc"] = 1
            score += 0.20

        # Row quantity bonus
        if len(table.rows) >= 2:
            score += 0.10

        return round(score, 2), col_map

    def _normalize_uom(self, uom_str: str) -> str:
        """Normalizes UOM string to common standard units."""
        u = uom_str.upper().strip()
        if not u:
            return "PCS"
        if u in ["PC", "PCS", "PIECE", "PIECES", "UNIT", "UNITS", "EA", "EACH"]:
            return "PCS"
        if u in ["NO", "NOS", "NUMBER", "NUMBERS"]:
            return "NOS"
        if u in ["MTR", "METER", "METERS", "M"]:
            return "MTR"
        if u in ["BX", "BOX", "BOXES", "PK", "PACK"]:
            return "BOX"
        if u in ["SET", "SETS"]:
            return "SET"
        if u in ["KG", "KGS", "KILOGRAM"]:
            return "KG"
        if u in ["LTR", "LITER", "LITERS", "L"]:
            return "LTR"
        return u
