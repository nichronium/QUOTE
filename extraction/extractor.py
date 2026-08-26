"""
Canonical Quote Extractor (General Evidence-Based Framework).
Implements the end-to-end General Document Understanding Framework:
DocumentProfile -> Region/Table Classification -> Primary Table Selection ->
Column/Row Semantic Inference -> Metadata Resolution -> Cross-Sheet Volume Tier Linking ->
Commercial Charge Extraction -> Financial Engine -> Validation & Reconciliation.
"""

from decimal import Decimal
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from core.canonical_quote import (
    AdditionalCharge,
    CanonicalQuote,
    ChargeType,
    DeliveryTerms,
    ExtractionMetadata,
    FieldEvidence,
    FieldStatus,
    HiddenRowPolicy,
    PaymentTerms,
    PriceTier,
    ProcessingMode,
    Provenance,
    QuoteItem,
    TaxComponent,
    quantize_currency,
)


from extraction.classifier import ColumnClassifier, RowClassifier
from extraction.commercial_charges import CommercialChargeExtractor
from extraction.document_profiler import DocumentProfiler
from extraction.financial_engine import FinancialEngine
from extraction.metadata_resolver import MetadataResolver
from extraction.normalizer import (
    detect_tax_rate,
    normalize_currency,
    normalize_decimal,
    normalize_uom,
    parse_flexible_date,
)
from extraction.reconciliation import QuoteReconciler
from extraction.semantic_registry import STANDARD_UOM_SET, TableType
from extraction.table_classifier import TableClassifier
from parsers.base import DocumentAST, ExtractedTable, RowType, TableRow



def get_cell_reference(row_idx: int, col_idx: int) -> str:
    col_str = ""
    n = col_idx + 1
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        col_str = chr(65 + remainder) + col_str
    return f"{col_str}{row_idx + 1}"


class QuoteExtractor:
    """Evidence-based extractor converting DocumentAST into validated CanonicalQuote with full provenance."""

    def __init__(self, hidden_row_policy: HiddenRowPolicy = HiddenRowPolicy.SMART_INCLUDE):
        self.col_classifier = ColumnClassifier()
        self.row_classifier = RowClassifier()
        self.reconciler = QuoteReconciler()
        self.metadata_resolver = MetadataResolver()
        self.financial_engine = FinancialEngine()
        self.table_classifier = TableClassifier()
        self.charge_extractor = CommercialChargeExtractor()
        self.hidden_row_policy = hidden_row_policy

    def extract(self, ast: DocumentAST) -> CanonicalQuote:
        # 1. Profile Document Structure
        profile = DocumentProfiler.profile_ast(ast)

        # 2. Classify Tables & Regions across all sheets
        item_tables: List[ExtractedTable] = []
        tier_tables: List[ExtractedTable] = []

        for table in ast.tables:
            t_type, t_conf = self.table_classifier.classify_table(table)
            if t_type == TableType.VOLUME_PRICING:
                tier_tables.append(table)
            elif t_type == TableType.MAIN_LINE_ITEMS:
                item_tables.append(table)
            elif t_type in [TableType.HISTORICAL_REFERENCE, TableType.SUMMARY_TOTALS, TableType.TERMS, TableType.NOTES]:
                continue
            else:
                # Fallback check
                headers_str = " ".join(table.headers).lower()
                if any(kw in headers_str for kw in ["min qty", "from qty", "tier"]):
                    tier_tables.append(table)
                elif any(kw in headers_str for kw in ["description", "item", "product", "particulars"]):
                    item_tables.append(table)

        # 3. Select Primary Line Items Table using evidence scoring
        primary_table = None
        if item_tables:
            sorted_candidates = sorted(item_tables, key=lambda t: self.table_classifier.score_main_item_table(t), reverse=True)
            primary_table = sorted_candidates[0]

        table_col_map: Dict[str, int] = {}
        if primary_table:
            table_col_map = self.col_classifier.classify_table_columns(primary_table)

        # 4. Resolve Document-level Metadata & Commercial Terms
        meta = self.metadata_resolver.resolve_metadata(ast, primary_table, table_col_map)

        # 5. Extract Line Items from the Primary Item Table
        items: List[QuoteItem] = []
        stated_grand_total = None
        stated_grand_total_evidence = None
        if primary_table:
            items, stated_grand_total, stated_grand_total_evidence = self._extract_items_from_primary_table(
                primary_table, table_col_map, ast.source_file_name
            )

        # Fallback: Scan text blocks across workbook if grand total wasn't in primary table footer
        if stated_grand_total is None:
            for page in ast.pages:
                for block in page.text_blocks:
                    block_low = block.text.lower()

                    if any(kw in block_low for kw in ["grand total", "total amount", "amount payable", "invoice total"]):
                        match = re.search(r"(?:grand\s+total|total\s+amount|amount\s+payable|invoice\s+total)[\s:₹$€\t-]*(\d+(?:,\d+)*(?:\.\d+)?)", block.text, re.IGNORECASE)
                        if match:
                            val = normalize_decimal(match.group(1))
                            if val > Decimal("0.0"):
                                stated_grand_total = val
                                stated_grand_total_evidence = FieldEvidence(
                                    raw_value=match.group(0).strip(),
                                    normalized_value=val,
                                    source_file=ast.source_file_name,
                                    sheet_name=page.sheet_name,
                                    extraction_method="text_block_scan",
                                    evidence_signals=["Extracted from document footer text block"],
                                    confidence=0.95,
                                    status=FieldStatus.CONFIRMED
                                )
                                break
                if stated_grand_total is not None:
                    break

        # 6. Extract Commercial Charges using dedicated charge extractor
        additional_charges = self.charge_extractor.extract_charges(ast)

        # 7. Extract and link volume pricing tiers across workbook
        self._link_volume_tiers_cross_sheet(items, tier_tables, ast)


        quote_id = f"Q-{ast.source_file_hash[:8].upper()}"

        # 8. Build FieldEvidence for metadata
        supplier_evidence = FieldEvidence(
            raw_value=meta.get("supplier_raw_name"),
            normalized_value=meta.get("supplier_raw_name"),
            source_file=ast.source_file_name,
            extraction_method="metadata_resolver",
            evidence_signals=["Extracted from document header/text block evidence"],
            confidence=0.95 if meta.get("supplier_raw_name") and meta.get("supplier_raw_name") != "Unknown Supplier" else 0.20,
            status=FieldStatus.CONFIRMED if meta.get("supplier_raw_name") and meta.get("supplier_raw_name") != "Unknown Supplier" else FieldStatus.MISSING
        )

        quote_num_evidence = FieldEvidence(
            raw_value=meta.get("quote_number"),
            normalized_value=meta.get("quote_number"),
            source_file=ast.source_file_name,
            extraction_method="metadata_resolver",
            evidence_signals=["Extracted from quote reference number pattern"],
            confidence=0.90 if meta.get("quote_number") else 0.0,
            status=FieldStatus.CONFIRMED if meta.get("quote_number") else FieldStatus.MISSING
        )

        quote_date_evidence = FieldEvidence(
            raw_value=str(meta.get("quote_date")) if meta.get("quote_date") else None,
            normalized_value=meta.get("quote_date"),
            source_file=ast.source_file_name,
            extraction_method="metadata_resolver",
            evidence_signals=["Extracted from date format parser"],
            confidence=0.90 if meta.get("quote_date") else 0.0,
            status=FieldStatus.CONFIRMED if meta.get("quote_date") else FieldStatus.MISSING
        )

        curr_val = meta.get("currency")
        currency_evidence = FieldEvidence(
            raw_value=curr_val,
            normalized_value=curr_val,
            source_file=ast.source_file_name,
            extraction_method="currency_normalizer" if curr_val else "missing_field",
            evidence_signals=["Resolved from currency symbol/text token"] if curr_val else ["Currency not specified in document"],
            confidence=0.95 if curr_val else 0.0,
            status=FieldStatus.CONFIRMED if curr_val else FieldStatus.MISSING
        )

        initial_quote = CanonicalQuote(
            quote_id=quote_id,
            supplier_raw_name=meta["supplier_raw_name"],
            quote_number=meta["quote_number"],
            quote_date=meta["quote_date"],
            valid_until=meta["valid_until"],
            currency=meta["currency"],
            payment_terms=meta["payment_terms"],
            delivery_terms=meta["delivery_terms"],
            additional_charges=additional_charges,
            items=items,
            supplier_evidence=supplier_evidence,
            quote_number_evidence=quote_num_evidence,
            quote_date_evidence=quote_date_evidence,
            currency_evidence=currency_evidence,
            stated_grand_total_evidence=stated_grand_total_evidence,
            extraction_metadata=ExtractionMetadata(
                source_file_name=ast.source_file_name,
                source_file_hash=ast.source_file_hash,
                parser_used=f"ast_{ast.file_type.lower()}_extractor",
                overall_confidence=1.0,
                warnings=[],
                processing_mode=ProcessingMode.LOCAL
            )
        )

        # 9. Run Reconciliation & Field-Level Confidence Calibration
        report = self.reconciler.reconcile(initial_quote, stated_grand_total=stated_grand_total)

        initial_quote.extraction_metadata.overall_confidence = report.calibrated_confidence
        initial_quote.extraction_metadata.warnings = report.warning_messages

        return initial_quote

    def _extract_items_from_primary_table(
        self, table: ExtractedTable, col_map: Dict[str, int], source_file_name: str
    ) -> Tuple[List[QuoteItem], Optional[Decimal], Optional[FieldEvidence]]:
        items: List[QuoteItem] = []
        stated_grand_total = None
        stated_grand_total_evidence = None
        line_counter = 0

        for row in table.rows:
            # Policy check: Hidden rows
            if row.is_hidden and self.hidden_row_policy == HiddenRowPolicy.EXCLUDE_ALL:
                continue

            row_type = self.row_classifier.classify_row(row, col_map)

            if row_type in [RowType.SUBTOTAL, RowType.GRAND_TOTAL]:
                val = normalize_decimal(row.raw_text)
                if val > Decimal("0.0") and "grand total" in row.raw_text.lower():
                    stated_grand_total = val
                    stated_grand_total_evidence = FieldEvidence(
                        raw_value=row.raw_text.strip(),
                        normalized_value=val,
                        source_file=source_file_name,
                        sheet_name=row.sheet_name or table.sheet_name,
                        cell_range=f"R{row.row_index}",
                        extraction_method="grand_total_row",
                        evidence_signals=["Extracted from table footer grand total row"],
                        confidence=0.95,
                        status=FieldStatus.CONFIRMED
                    )
                continue

            elif row_type in [RowType.FREIGHT, RowType.PACKING, RowType.INSURANCE, RowType.TAX, RowType.NOTE, RowType.HEADER, RowType.VOLUME_TIER]:
                continue

            elif row_type == RowType.CONTINUATION:
                if items:
                    desc_col = col_map.get("description", 0)
                    continuation_text = row.cells[desc_col].strip() if desc_col < len(row.cells) else ""
                    if continuation_text:
                        items[-1].raw_description += f"\n{continuation_text}"
                continue

            item = self._row_to_quote_item(row, col_map, line_counter, table.sheet_name, source_file_name)
            if item:
                items.append(item)
                line_counter += 1

        return items, stated_grand_total, stated_grand_total_evidence

    def _link_volume_tiers_cross_sheet(
        self, items: List[QuoteItem], tier_tables: List[ExtractedTable], ast: DocumentAST
    ):
        """Links pricing tiers from all VOLUME_PRICING tables across workbook to matching line items with provenance."""
        if not tier_tables or not items:
            return

        for t_table in tier_tables:
            col_sku = -1
            col_min = -1
            col_max = -1
            col_price = -1

            for c_idx, h in enumerate(t_table.headers):
                h_low = h.lower()
                if "sku" in h_low or "part" in h_low or "code" in h_low:
                    col_sku = c_idx
                elif "min" in h_low or "from" in h_low:
                    col_min = c_idx
                elif "max" in h_low or "to" in h_low:
                    col_max = c_idx
                elif "price" in h_low or "rate" in h_low:
                    col_price = c_idx

            if col_min == -1:
                col_min = 0
            if col_max == -1:
                col_max = 1 if len(t_table.headers) > 1 else -1
            if col_price == -1:
                col_price = 2 if len(t_table.headers) > 2 else -1

            # Check if there's a section title for the whole table (e.g. 'VOLUME PRICING — CABLE-4SQ')
            table_target_sku = None
            for p in ast.pages:
                if p.sheet_name == t_table.sheet_name:
                    for b in p.text_blocks:
                        if any(kw in b.text.lower() for kw in ["volume pricing", "tier pricing"]):
                            for item in items:
                                if item.supplier_part_number and item.supplier_part_number.lower() in b.text.lower():
                                    table_target_sku = item.supplier_part_number
                                    break

            for r in t_table.rows:
                if len(r.cells) <= max(col_min, col_price if col_price != -1 else 0):
                    continue

                row_sku = r.cells[col_sku].strip() if col_sku != -1 and col_sku < len(r.cells) else table_target_sku
                min_str = r.cells[col_min].strip() if col_min < len(r.cells) else ""
                max_str = r.cells[col_max].strip() if col_max != -1 and col_max < len(r.cells) else ""
                price_str = r.cells[col_price].strip() if col_price != -1 and col_price < len(r.cells) else ""

                if any(kw in min_str.lower() for kw in ["freight", "packing", "insurance", "grand total", "subtotal", "total", "terms", "notes", "delivery", "shipping"]):
                    continue

                min_val = normalize_decimal(min_str, default=Decimal("0.0"))
                max_val = normalize_decimal(max_str) if max_str and max_str.isdigit() else None
                price_val = normalize_decimal(price_str, default=Decimal("0.0"))


                if price_val > Decimal("0.0"):
                    matching_item = None
                    if row_sku:
                        matching_item = next((it for it in items if it.supplier_part_number == row_sku or row_sku.lower() in it.raw_description.lower()), None)
                    if not matching_item and len(items) == 1:
                        matching_item = items[0]

                    if matching_item:
                        tier_prov = Provenance(
                            page_number=r.page_number,
                            sheet_name=t_table.sheet_name,
                            row_idx=r.row_index,
                            cell_ref=get_cell_reference(r.row_index, col_price if col_price != -1 else 0),
                            source_text=f"Min: {min_str}, Max: {max_str}, Price: {price_str}"
                        )
                        min_ev = FieldEvidence(
                            raw_value=min_str,
                            normalized_value=min_val,
                            source_file=ast.source_file_name,
                            sheet_name=t_table.sheet_name,
                            cell_range=get_cell_reference(r.row_index, col_min),
                            extraction_method="tier_min_qty_cell",
                            evidence_signals=["Extracted from tier table min qty column"],
                            confidence=0.95,
                            status=FieldStatus.CONFIRMED
                        )
                        max_ev = FieldEvidence(
                            raw_value=max_str,
                            normalized_value=max_val,
                            source_file=ast.source_file_name,
                            sheet_name=t_table.sheet_name,
                            cell_range=get_cell_reference(r.row_index, col_max) if col_max != -1 else None,
                            extraction_method="tier_max_qty_cell",
                            evidence_signals=["Extracted from tier table max qty column"] if max_str else ["Open-ended upper tier"],
                            confidence=0.95 if max_str else 0.90,
                            status=FieldStatus.CONFIRMED if max_str else FieldStatus.INFERRED
                        )
                        price_ev = FieldEvidence(
                            raw_value=price_str,
                            normalized_value=price_val,
                            source_file=ast.source_file_name,
                            sheet_name=t_table.sheet_name,
                            cell_range=get_cell_reference(r.row_index, col_price if col_price != -1 else 0),
                            extraction_method="tier_price_cell",
                            evidence_signals=["Extracted from tier table unit price column"],
                            confidence=0.95,
                            status=FieldStatus.CONFIRMED
                        )

                        tier = PriceTier(
                            min_qty=min_val,
                            max_qty=max_val,
                            unit_price=price_val,
                            provenance=tier_prov,
                            min_qty_evidence=min_ev,
                            max_qty_evidence=max_ev,
                            unit_price_evidence=price_ev
                        )
                        if tier not in matching_item.price_tiers:
                            matching_item.price_tiers.append(tier)

    def _row_to_quote_item(
        self, row: TableRow, col_map: Dict[str, int], line_index: int, sheet_name: Optional[str], source_file_name: str
    ) -> Optional[QuoteItem]:
        cells = row.cells
        if not cells:
            return None

        def get_cell(key: str) -> Optional[str]:
            if key in col_map and col_map[key] < len(cells):
                val = cells[col_map[key]].strip()
                return val if val else None
            return None

        description = get_cell("description")
        if not description or description.lower() in ["min qty", "max qty", "unit price", "subtotal", "grand total", "description", "item"]:
            return None

        qty_str = get_cell("qty")
        price_str = get_cell("unit_price")

        quoted_qty = normalize_decimal(qty_str, default=None)
        if quoted_qty is not None and quoted_qty <= Decimal("0.0"):
            quoted_qty = None

        unit_price = normalize_decimal(price_str, default=None)
        if unit_price is None and "unit_price" not in col_map and len(cells) > 1:
            for i, c in enumerate(cells):
                if i != col_map.get("qty") and i != col_map.get("description"):
                    val = normalize_decimal(c, default=None)
                    if val is not None and val > Decimal("0.0"):
                        unit_price = val
                        price_str = c
                        break

        uom_str = get_cell("uom")
        uom = normalize_uom(uom_str, default=None)
        part_number = (
            get_cell("supplier_part_number")
            or get_cell("manufacturer_part_number")
            or get_cell("internal_sku")
            or get_cell("part_number")
        )
        hsn_code = get_cell("hsn")
        discount = normalize_decimal(get_cell("discount"), default=Decimal("0.0"))

        # Compound Tax Handling (CGST, SGST, IGST, CESS, or unified Tax)
        tax_components: List[TaxComponent] = []
        cgst_val = normalize_decimal(get_cell("cgst"), default=None)
        sgst_val = normalize_decimal(get_cell("sgst"), default=None)
        igst_val = normalize_decimal(get_cell("igst"), default=None)
        cess_val = normalize_decimal(get_cell("cess"), default=None)
        unified_tax_val = normalize_decimal(get_cell("tax"), default=None)

        if cgst_val is not None and cgst_val > Decimal("0.0"):
            cgst_ref = get_cell_reference(row.row_index, col_map["cgst"]) if "cgst" in col_map else None
            tax_components.append(TaxComponent(
                tax_type="CGST",
                rate_pct=cgst_val,
                evidence=FieldEvidence(
                    raw_value=get_cell("cgst"),
                    normalized_value=cgst_val,
                    source_file=source_file_name,
                    sheet_name=sheet_name,
                    cell_range=cgst_ref,
                    extraction_method="column_cell",
                    evidence_signals=[f"CGST rate {cgst_val}%"],
                    confidence=0.95,
                    status=FieldStatus.CONFIRMED
                )
            ))

        if sgst_val is not None and sgst_val > Decimal("0.0"):
            sgst_ref = get_cell_reference(row.row_index, col_map["sgst"]) if "sgst" in col_map else None
            tax_components.append(TaxComponent(
                tax_type="SGST",
                rate_pct=sgst_val,
                evidence=FieldEvidence(
                    raw_value=get_cell("sgst"),
                    normalized_value=sgst_val,
                    source_file=source_file_name,
                    sheet_name=sheet_name,
                    cell_range=sgst_ref,
                    extraction_method="column_cell",
                    evidence_signals=[f"SGST rate {sgst_val}%"],
                    confidence=0.95,
                    status=FieldStatus.CONFIRMED
                )
            ))

        if igst_val is not None and igst_val > Decimal("0.0"):
            igst_ref = get_cell_reference(row.row_index, col_map["igst"]) if "igst" in col_map else None
            tax_components.append(TaxComponent(
                tax_type="IGST",
                rate_pct=igst_val,
                evidence=FieldEvidence(
                    raw_value=get_cell("igst"),
                    normalized_value=igst_val,
                    source_file=source_file_name,
                    sheet_name=sheet_name,
                    cell_range=igst_ref,
                    extraction_method="column_cell",
                    evidence_signals=[f"IGST rate {igst_val}%"],
                    confidence=0.95,
                    status=FieldStatus.CONFIRMED
                )
            ))

        if cess_val is not None and cess_val > Decimal("0.0"):
            cess_ref = get_cell_reference(row.row_index, col_map["cess"]) if "cess" in col_map else None
            tax_components.append(TaxComponent(
                tax_type="CESS",
                rate_pct=cess_val,
                evidence=FieldEvidence(
                    raw_value=get_cell("cess"),
                    normalized_value=cess_val,
                    source_file=source_file_name,
                    sheet_name=sheet_name,
                    cell_range=cess_ref,
                    extraction_method="column_cell",
                    evidence_signals=[f"CESS rate {cess_val}%"],
                    confidence=0.95,
                    status=FieldStatus.CONFIRMED
                )
            ))

        # Compute total aggregate tax rate
        tax_rate: Optional[Decimal] = None
        if tax_components:
            tax_rate = sum(c.rate_pct for c in tax_components)
        elif unified_tax_val is not None:
            tax_rate = unified_tax_val
            tax_components.append(TaxComponent(
                tax_type="GST",
                rate_pct=tax_rate,
                evidence=FieldEvidence(
                    raw_value=get_cell("tax"),
                    normalized_value=tax_rate,
                    source_file=source_file_name,
                    sheet_name=sheet_name,
                    cell_range=get_cell_reference(row.row_index, col_map["tax"]) if "tax" in col_map else None,
                    extraction_method="column_cell",
                    evidence_signals=[f"Unified tax rate {tax_rate}%"],
                    confidence=0.95,
                    status=FieldStatus.CONFIRMED
                )
            ))
        else:
            detected_rate = detect_tax_rate(row.raw_text)
            if detected_rate is not None and detected_rate > Decimal("0.0"):
                tax_rate = detected_rate
                tax_components.append(TaxComponent(
                    tax_type="GST",
                    rate_pct=tax_rate,
                    evidence=FieldEvidence(
                        raw_value=str(tax_rate),
                        normalized_value=tax_rate,
                        source_file=source_file_name,
                        sheet_name=sheet_name,
                        cell_range=None,
                        extraction_method="regex_tax_detection",
                        evidence_signals=[f"Inferred tax rate {tax_rate}% from row text"],
                        confidence=0.85,
                        status=FieldStatus.INFERRED
                    )
                ))

        lead_time = None
        lead_str = get_cell("lead_time")
        if lead_str:
            lead_match = re.search(r"(\d+)", lead_str)
            if lead_match:
                lead_time = int(lead_match.group(1))

        desc_col_idx = col_map.get("description", 0)
        cell_ref = get_cell_reference(row.row_index, desc_col_idx)

        provenance = Provenance(
            page_number=row.page_number,
            sheet_name=sheet_name,
            row_idx=row.row_index,
            col_idx=desc_col_idx,
            cell_ref=cell_ref,
            source_text=description,
            is_hidden_row=row.is_hidden
        )

        item_confidence = self._calculate_item_confidence(
            description=description,
            qty=quoted_qty,
            uom=uom,
            price=unit_price,
            tax_rate=tax_rate,
            part_no=part_number
        )

        # Build field-level evidence records for this item
        signals = ["Extracted from primary line-items table"]
        if row.is_hidden:
            signals.append("Extracted from hidden spreadsheet row")

        desc_ev = FieldEvidence(
            raw_value=description,
            normalized_value=description,
            source_file=source_file_name,
            sheet_name=sheet_name,
            cell_range=cell_ref,
            extraction_method="column_cell",
            evidence_signals=signals,
            confidence=0.95,
            status=FieldStatus.CONFIRMED
        )

        if quoted_qty is not None:
            qty_ev = FieldEvidence(
                raw_value=qty_str,
                normalized_value=quoted_qty,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=get_cell_reference(row.row_index, col_map.get("qty", 0)) if "qty" in col_map else None,
                extraction_method="column_cell" if "qty" in col_map else "row_numeric_scan",
                evidence_signals=["Extracted from quantity column"],
                confidence=0.95,
                status=FieldStatus.CONFIRMED
            )
        else:
            qty_ev = FieldEvidence(
                raw_value=qty_str,
                normalized_value=None,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=None,
                extraction_method="missing_field",
                evidence_signals=["Quantity not specified in document"],
                confidence=0.0,
                status=FieldStatus.MISSING
            )

        if uom is not None:
            uom_ev = FieldEvidence(
                raw_value=uom_str,
                normalized_value=uom,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=get_cell_reference(row.row_index, col_map.get("uom", 0)) if "uom" in col_map else None,
                extraction_method="uom_registry_normalization",
                evidence_signals=[f"Normalized UOM '{uom_str}' -> '{uom}'"],
                confidence=0.95 if uom in STANDARD_UOM_SET else 0.70,
                status=FieldStatus.CONFIRMED if uom in STANDARD_UOM_SET else FieldStatus.INFERRED
            )
        else:
            uom_ev = FieldEvidence(
                raw_value=uom_str,
                normalized_value=None,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=None,
                extraction_method="missing_field",
                evidence_signals=["UOM not specified in document"],
                confidence=0.0,
                status=FieldStatus.MISSING
            )

        if unit_price is not None:
            price_ev = FieldEvidence(
                raw_value=price_str,
                normalized_value=unit_price,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=get_cell_reference(row.row_index, col_map.get("unit_price", 0)) if "unit_price" in col_map else None,
                extraction_method="column_cell" if "unit_price" in col_map else "row_numeric_scan",
                evidence_signals=["Extracted from unit rate/price column"],
                confidence=0.95 if unit_price > Decimal("0.0") else 0.20,
                status=FieldStatus.CONFIRMED if unit_price > Decimal("0.0") else FieldStatus.MISSING
            )
        else:
            price_ev = FieldEvidence(
                raw_value=price_str,
                normalized_value=None,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=None,
                extraction_method="missing_field",
                evidence_signals=["Unit price not specified in document"],
                confidence=0.0,
                status=FieldStatus.MISSING
            )

        discount_ev = FieldEvidence(
            raw_value=get_cell("discount"),
            normalized_value=discount,
            source_file=source_file_name,
            sheet_name=sheet_name,
            cell_range=get_cell_reference(row.row_index, col_map.get("discount", 0)) if "discount" in col_map else None,
            extraction_method="column_cell" if "discount" in col_map else "zero_default",
            evidence_signals=["Extracted from discount column"] if "discount" in col_map else ["No discount applied (0%)"],
            confidence=0.95,
            status=FieldStatus.CONFIRMED
        )

        # Total tax evidence
        if tax_rate is not None:
            tax_sig = [f"Total aggregate tax rate {tax_rate}%"]
            if tax_components:
                tax_sig.append(f"Components: {', '.join(f'{c.tax_type} {c.rate_pct}%' for c in tax_components)}")
            tax_ev = FieldEvidence(
                raw_value=get_cell("tax") or str(tax_rate),
                normalized_value=tax_rate,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=get_cell_reference(row.row_index, col_map.get("tax", 0)) if "tax" in col_map else None,
                extraction_method="compound_tax_sum" if len(tax_components) > 1 else ("column_cell" if "tax" in col_map else "regex_tax_detection"),
                evidence_signals=tax_sig,
                confidence=0.95 if (tax_components or "tax" in col_map) else 0.85,
                status=FieldStatus.CONFIRMED if (tax_components or "tax" in col_map) else FieldStatus.INFERRED
            )
        else:
            tax_ev = FieldEvidence(
                raw_value=get_cell("tax"),
                normalized_value=None,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=None,
                extraction_method="missing_field",
                evidence_signals=["Tax rate not specified in document"],
                confidence=0.0,
                status=FieldStatus.MISSING
            )

        if quoted_qty is not None and unit_price is not None:
            disc_mult = (Decimal("1.0") - (discount / Decimal("100.0"))) if discount is not None else Decimal("1.0")
            tax_mult = (Decimal("1.0") + (tax_rate / Decimal("100.0"))) if tax_rate is not None else Decimal("1.0")
            line_landed = quantize_currency((quoted_qty * unit_price * disc_mult) * tax_mult)
            line_tot_ev = FieldEvidence(
                raw_value=str(line_landed),
                normalized_value=line_landed,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=cell_ref,
                extraction_method="financial_engine_decimal",
                evidence_signals=["Computed by pure Decimal line arithmetic invariant"],
                confidence=0.99,
                status=FieldStatus.CONFIRMED
            )
        else:
            line_landed = None
            line_tot_ev = FieldEvidence(
                raw_value=None,
                normalized_value=None,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=cell_ref,
                extraction_method="missing_field",
                evidence_signals=["Line landed cost cannot be calculated due to missing quantity or unit price"],
                confidence=0.0,
                status=FieldStatus.MISSING
            )

        sku_ev = None
        sku_col_idx = (
            col_map.get("supplier_part_number")
            if "supplier_part_number" in col_map
            else col_map.get(
                "manufacturer_part_number",
                col_map.get("internal_sku", col_map.get("part_number", 0)),
            )
        )
        if part_number:
            sku_ev = FieldEvidence(
                raw_value=part_number,
                normalized_value=part_number,
                source_file=source_file_name,
                sheet_name=sheet_name,
                cell_range=get_cell_reference(row.row_index, sku_col_idx),
                extraction_method="column_cell",
                evidence_signals=["Extracted from SKU / part number column"],
                confidence=0.95,
                status=FieldStatus.CONFIRMED
            )
        else:
            # Conservative SKU prefix extraction fallback from item description (A3)
            sku_match = re.match(r"^([A-Z0-9]{2,}[-_/][A-Z0-9-_/]+|[A-Z]{2,}\d{2,}(?:[-_/][A-Z0-9]+)*)\b", description.strip())
            if sku_match:
                candidate_sku = sku_match.group(1).strip()
                # Ensure candidate is not a common unit/currency/date/quantity/generic word
                is_invalid = (
                    candidate_sku.upper() in STANDARD_UOM_SET or
                    candidate_sku.upper() in ["INR", "USD", "EUR", "GBP", "ITEM", "PART", "CODE", "SPEC", "SIZE", "TYPE"] or
                    bool(re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}$", candidate_sku)) or
                    bool(re.match(r"^\d+$", candidate_sku))
                )
                if not is_invalid and len(candidate_sku) >= 3:
                    part_number = candidate_sku
                    sku_ev = FieldEvidence(
                        raw_value=candidate_sku,
                        normalized_value=candidate_sku,
                        source_file=source_file_name,
                        sheet_name=sheet_name,
                        cell_range=cell_ref,
                        extraction_method="description_prefix_regex",
                        evidence_signals=["Inferred leading SKU pattern from item description"],
                        confidence=0.85,
                        status=FieldStatus.INFERRED
                    )

        return QuoteItem(
            line_index=line_index,
            raw_description=description,
            supplier_part_number=part_number,
            hsn_sac_code=hsn_code,
            quoted_qty=quoted_qty,
            quoted_uom=uom,
            unit_price=unit_price,
            discount_pct=discount,
            tax_rate_pct=tax_rate if tax_rate is not None else Decimal("0.0"),
            tax_components=tax_components,
            lead_time_days=lead_time,
            confidence_score=item_confidence,
            provenance=provenance,
            description_evidence=desc_ev,
            supplier_part_number_evidence=sku_ev,
            quoted_qty_evidence=qty_ev,
            quoted_uom_evidence=uom_ev,
            unit_price_evidence=price_ev,
            discount_evidence=discount_ev,
            tax_rate_evidence=tax_ev,
            line_total_evidence=line_tot_ev
        )

    def _calculate_item_confidence(
        self,
        description: str,
        qty: Optional[Decimal],
        uom: Optional[str],
        price: Optional[Decimal],
        tax_rate: Optional[Decimal],
        part_no: Optional[str]
    ) -> float:
        score = 0.0

        if description and len(description.strip()) >= 3 and not description.strip().isdigit():
            score += 0.25

        if price is not None and price > Decimal("0.0"):
            score += 0.25

        if qty is not None and qty > Decimal("0.0"):
            score += 0.15

        if uom and uom.upper() in STANDARD_UOM_SET:
            score += 0.15
        elif uom:
            score += 0.05

        if tax_rate is not None and tax_rate > Decimal("0.0"):
            score += 0.10

        if part_no and len(part_no.strip()) >= 2:
            score += 0.05

        score += 0.05
        return round(min(1.0, max(0.1, score)), 2)

