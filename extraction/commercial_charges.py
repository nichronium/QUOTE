"""
Dedicated Commercial Charge Extractor.
Extracts freight, packing, insurance, tooling, and other quote-level surcharges
from both structured tabular grids and unstructured text blocks.
Ensures amounts and tax percentages are extracted independently without digit concatenation.
Ignores decoy, internal, and ground truth sheets.
"""

from decimal import Decimal
import re
from typing import Dict, List, Optional, Set, Tuple

from core.canonical_quote import AdditionalCharge, ChargeType, FieldEvidence, FieldStatus, Provenance

from extraction.normalizer import detect_tax_rate, normalize_decimal
from extraction.semantic_registry import CHARGE_PATTERNS
from parsers.base import DocumentAST, ExtractedTable, TextBlock


class CommercialChargeExtractor:
    """Extracts quote-level commercial surcharges with independent amount & tax isolation."""

    def extract_charges(self, ast: DocumentAST) -> List[AdditionalCharge]:
        charges: List[AdditionalCharge] = []
        seen_keys: Set[Tuple[ChargeType, Decimal]] = set()

        # 1. Extract from Tabular Structures (Terms & Charges tables, Footer tables)
        for table in ast.tables:
            table_charges = self._extract_from_table(table)
            for ch in table_charges:
                key = (ch.charge_type, ch.amount)
                if key not in seen_keys:
                    charges.append(ch)
                    seen_keys.add(key)

        # 2. Extract from Text Blocks
        for page in ast.pages:
            for block in page.text_blocks:
                block_charges = self._extract_from_text(block.text, page.page_number, page.sheet_name)
                for ch in block_charges:
                    key = (ch.charge_type, ch.amount)
                    if key not in seen_keys:
                        charges.append(ch)
                        seen_keys.add(key)

        return charges


    def _extract_from_table(self, table: ExtractedTable) -> List[AdditionalCharge]:
        charges: List[AdditionalCharge] = []
        headers_str = " ".join(table.headers).lower()

        col_amt_idx = -1
        col_tax_idx = -1

        for idx, h in enumerate(table.headers):
            h_low = h.lower()
            if "amount" in h_low or "value" in h_low or "charge" in h_low or "rate" in h_low:
                col_amt_idx = idx
            elif "tax" in h_low or "gst" in h_low:
                col_tax_idx = idx

        for row in table.rows:
            row_text = row.raw_text
            cells = [c.strip() for c in row.cells if c.strip()]
            if not cells:
                continue

            first_cell = cells[0].lower()
            charge_type = self._classify_charge_type(first_cell)

            if charge_type:
                amount: Optional[Decimal] = None
                tax_rate: Decimal = Decimal("0.0")

                if col_amt_idx != -1 and col_amt_idx < len(row.cells) and row.cells[col_amt_idx].strip():
                    amount = normalize_decimal(row.cells[col_amt_idx])
                else:
                    for c in cells[1:]:
                        val = normalize_decimal(c)
                        if val is not None and val > Decimal("0.0"):
                            amount = val
                            break

                if col_tax_idx != -1 and col_tax_idx < len(row.cells) and row.cells[col_tax_idx].strip():
                    tax_rate = normalize_decimal(row.cells[col_tax_idx], default=Decimal("0.0")) or Decimal("0.0")
                else:
                    tax_rate = detect_tax_rate(row_text) or Decimal("0.0")

                if amount is not None and amount > Decimal("0.0"):
                    sheet = row.sheet_name or table.sheet_name
                    prov = Provenance(
                        page_number=row.page_number,
                        sheet_name=sheet,
                        row_idx=row.row_index,
                        source_text=row_text
                    )
                    amt_ev = FieldEvidence(
                        raw_value=str(amount),
                        normalized_value=amount,
                        sheet_name=sheet,
                        cell_range=f"R{row.row_index}",
                        extraction_method="table_column" if col_amt_idx != -1 else "row_scan",
                        evidence_signals=[f"Extracted charge amount for {charge_type.value}"],
                        confidence=0.95,
                        status=FieldStatus.CONFIRMED
                    )
                    type_ev = FieldEvidence(
                        raw_value=first_cell,
                        normalized_value=charge_type.value,
                        sheet_name=sheet,
                        cell_range=f"R{row.row_index}",
                        extraction_method="keyword_pattern",
                        evidence_signals=[f"Matched pattern for {charge_type.value}"],
                        confidence=0.95,
                        status=FieldStatus.CONFIRMED
                    )
                    tax_ev = FieldEvidence(
                        raw_value=str(tax_rate),
                        normalized_value=tax_rate if tax_rate > Decimal("0.0") else None,
                        sheet_name=sheet,
                        cell_range=f"R{row.row_index}",
                        extraction_method="column_cell" if col_tax_idx != -1 else "regex_tax_detection",
                        evidence_signals=[f"Tax rate {tax_rate}% detected on charge"] if tax_rate > Decimal("0.0") else ["No explicit tax detected on charge"],
                        confidence=0.95 if col_tax_idx != -1 else (0.85 if tax_rate > Decimal("0.0") else 0.50),
                        status=FieldStatus.CONFIRMED if col_tax_idx != -1 else (FieldStatus.INFERRED if tax_rate > Decimal("0.0") else FieldStatus.MISSING)
                    )
                    charges.append(AdditionalCharge(
                        charge_type=charge_type,
                        amount=amount,
                        tax_rate_pct=tax_rate,
                        raw_text=row_text.strip(),
                        provenance=prov,
                        amount_evidence=amt_ev,
                        charge_type_evidence=type_ev,
                        tax_rate_evidence=tax_ev
                    ))

        return charges

    def _extract_from_text(self, text: str, page_number: Optional[int], sheet_name: Optional[str]) -> List[AdditionalCharge]:
        charges: List[AdditionalCharge] = []
        lines = text.split("\n")

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue

            parts = [p.strip() for p in re.split(r"[\t:]+", line_str) if p.strip()]

            for i, part in enumerate(parts):
                part_low = part.lower()
                charge_type = self._classify_charge_type(part_low)
                
                if charge_type:
                    amount = Decimal("0.0")
                    tax_rate = Decimal("0.0")

                    if i + 1 < len(parts):
                        val = normalize_decimal(parts[i + 1], default=Decimal("0.0"))
                        if val > Decimal("0.0"):
                            amount = val
                    
                    if amount <= Decimal("0.0"):
                        match = re.search(r"\b(?:" + "|".join(CHARGE_PATTERNS.get(charge_type.value, [])) + r")[\s#.:/-]*₹?\s*(\d+(?:\.\d+)?)", line_str, re.IGNORECASE)
                        if match:
                            amount = Decimal(match.group(1))

                    if i + 2 < len(parts):
                        tax_val = normalize_decimal(parts[i + 2], default=Decimal("0.0"))
                        if tax_val > Decimal("0.0") and tax_val <= Decimal("40.0"):
                            tax_rate = tax_val

                    if tax_rate <= Decimal("0.0"):
                        tax_rate = detect_tax_rate(line_str) or Decimal("0.0")

                    if amount > Decimal("0.0"):
                        prov = Provenance(
                            page_number=page_number,
                            sheet_name=sheet_name,
                            source_text=line_str
                        )
                        amt_ev = FieldEvidence(
                            raw_value=str(amount),
                            normalized_value=amount,
                            sheet_name=sheet_name,
                            extraction_method="text_pattern_scan",
                            evidence_signals=[f"Extracted charge amount for {charge_type.value} from text block"],
                            confidence=0.90,
                            status=FieldStatus.CONFIRMED
                        )
                        type_ev = FieldEvidence(
                            raw_value=part,
                            normalized_value=charge_type.value,
                            sheet_name=sheet_name,
                            extraction_method="keyword_pattern",
                            evidence_signals=[f"Matched pattern for {charge_type.value}"],
                            confidence=0.90,
                            status=FieldStatus.CONFIRMED
                        )
                        tax_ev = FieldEvidence(
                            raw_value=str(tax_rate),
                            normalized_value=tax_rate if tax_rate > Decimal("0.0") else None,
                            sheet_name=sheet_name,
                            extraction_method="regex_tax_detection",
                            evidence_signals=[f"Tax rate {tax_rate}% detected on charge"] if tax_rate > Decimal("0.0") else ["No explicit tax detected on charge"],
                            confidence=0.85 if tax_rate > Decimal("0.0") else 0.50,
                            status=FieldStatus.INFERRED if tax_rate > Decimal("0.0") else FieldStatus.MISSING
                        )
                        charges.append(AdditionalCharge(
                            charge_type=charge_type,
                            amount=amount,
                            tax_rate_pct=tax_rate,
                            raw_text=line_str,
                            provenance=prov,
                            amount_evidence=amt_ev,
                            charge_type_evidence=type_ev,
                            tax_rate_evidence=tax_ev
                        ))
                        break


        return charges

    def _classify_charge_type(self, text: str) -> Optional[ChargeType]:
        text_clean = text.lower().strip()
        for c_type_str, patterns in CHARGE_PATTERNS.items():
            for pat in patterns:
                if re.search(r"\b" + pat + r"\b", text_clean, re.IGNORECASE) or pat == text_clean:
                    if c_type_str == "FREIGHT":
                        return ChargeType.FREIGHT
                    elif c_type_str == "PACKING":
                        return ChargeType.PACKING
                    elif c_type_str == "INSURANCE":
                        return ChargeType.INSURANCE
                    elif c_type_str == "TOOLING":
                        return ChargeType.TOOLING
                    else:
                        return ChargeType.OTHER
        return None
