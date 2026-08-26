"""
Comprehensive Document Metadata & Key-Value Resolver.
Extracts Document Metadata (Supplier, Quote No, Dates, Commercial Terms)
from structured Key-Value pairs, adjacent table cells, and free-text headers,
with exact cell/region provenance.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from core.canonical_quote import (
    AdditionalCharge,
    ChargeType,
    DeliveryTerms,
    PaymentTerms,
    Provenance,
)
from extraction.normalizer import (
    detect_tax_rate,
    normalize_currency,
    normalize_decimal,
    parse_flexible_date,
)
from parsers.base import DocumentAST, ExtractedTable, TableRow, TextBlock


class MetadataResolver:
    """Evidence-based metadata resolver for quotation documents."""

    SUPPLIER_KEY_PATTERNS = [
        r"^(?:supplier\s*name|supplier|vendor\s*name|vendor|seller\s*name|seller|company\s*name|company|party\s*name|party|quoted\s*by|m/s|from)$",
        r"^(?:supplier|vendor|seller|company|party)\b"
    ]

    QUOTE_NUM_KEY_PATTERNS = [
        r"^(?:quote\s*no\.?|quotation\s*no\.?|quote\s*#|quote\s*num|ref\s*no\.?|reference\s*no\.?|quotation\s*ref|quote\s*ref|estimate\s*no\.?|inquiry\s*no\.?|rfq\s*no\.?)$",
        r"^(?:quote|quotation|ref)\s*(?:no|num|#|\.)"
    ]

    DATE_KEY_PATTERNS = [
        r"^(?:quote\s*date|quotation\s*date|dated|date|doc\s*date|po\s*date|rfq\s*date)$",
        r"^(?:date|dated)\b"
    ]

    VALIDITY_KEY_PATTERNS = [
        r"^(?:valid\s*(?:till|until)|validity|valid\s*up\s*to)$"
    ]

    INVALID_SUPPLIER_VALUES = {
        "name", "supplier", "vendor", "company", "party", "none", "null", "unknown",
        "sheet1", "sheet2", "sheet", "quotation", "quote", "invoice", "page 1",
        "clean quote", "quote details", "company info", "sr.", "sr no",
        # Document-title patterns that are NOT a supplier name
        "supplier quotation", "vendor quotation", "vendor quote", "price list",
        "rate list", "price quotation", "commercial offer", "proforma invoice",
        "purchase order", "request for quotation", "rfq response",
    }

    # Prefixes that indicate a cell is a label/key, not a supplier name
    LABEL_PREFIXES = re.compile(
        r"^(supplier|vendor|company|party|quoted\s*by|from|to|attn|ref|reference|"
        r"quote|quotation|date|dated|valid|invoice|po|rfq|gstin|pan|email|phone|"
        r"payment|delivery|contact|address|city|state|country|pin|tel|fax)\b",
        re.IGNORECASE
    )

    # Document-title single-cell patterns (header banners, not supplier names)
    DOCUMENT_TITLE_PATTERNS = re.compile(
        r"^(supplier\s+quotation|vendor\s+quote|price\s+list|rate\s+list|"
        r"commercial\s+offer|proforma\s+invoice|purchase\s+order|quotation\s+form|"
        r"request\s+for\s+quotation|rfq\s+response|price\s+quotation|"
        r"quotation|quotations?|invoice|estimate|offer|tender)$",
        re.IGNORECASE
    )

    TRUNCATION_KEYWORDS = [
        "date", "dated", "quote", "quotation", "ref", "valid", "validity",
        "gst", "gstin", "pan", "email", "phone", "attn", "to", "payment"
    ]

    def resolve_metadata(
        self, ast: DocumentAST, primary_table: Optional[ExtractedTable], col_map: Dict[str, int]
    ) -> Dict[str, Any]:
        """Resolves all document-level metadata fields with supporting evidence."""
        kv_pairs = self._extract_key_value_pairs(ast, primary_table)

        supplier = self._resolve_supplier(ast, primary_table, col_map, kv_pairs)
        quote_number = self._resolve_quote_number(ast, primary_table, col_map, kv_pairs)
        quote_date, valid_until = self._resolve_dates(ast, primary_table, col_map, kv_pairs)
        currency = self._resolve_currency(ast, primary_table, col_map, kv_pairs)
        payment_terms, delivery_terms = self._resolve_commercial_terms(ast, primary_table, kv_pairs)

        return {
            "supplier_raw_name": supplier,
            "quote_number": quote_number,
            "quote_date": quote_date,
            "valid_until": valid_until,
            "currency": currency,
            "payment_terms": payment_terms,
            "delivery_terms": delivery_terms
        }

    def _extract_key_value_pairs(
        self, ast: DocumentAST, table: Optional[ExtractedTable]
    ) -> List[Tuple[str, str, str]]:
        """
        Extracts (key, value, source) pairs from text blocks and table rows.

        Priority rule: each tab-delimited part is first tested for the pattern
        "SomeLabel: SomeValue". If a part contains a colon, split it as its own
        KV pair rather than treating adjacent tab-parts as (k, v).
        """
        pairs: List[Tuple[str, str, str]] = []

        def _extract_from_parts(parts: List[str], source: str) -> List[Tuple[str, str, str]]:
            """
            Parses a list of tab-delimited parts into (key, value) pairs.

            Handles three patterns:
            1. "Key: Value" in a single part  → (Key, Value)
            2. "Key:"  + next part "Value"    → (Key, Value)   [label in col A, value in col B]
            3. "Key" + "Value" (no colons)    → (Key, Value)   [adjacent plain values]
            """
            result = []
            i = 0
            while i < len(parts):
                part = parts[i]
                if ":" in part:
                    k, v = part.split(":", 1)
                    k, v = k.strip(), v.strip()
                    if k and v:
                        # Self-contained "Key: Value"
                        result.append((k, v, source))
                        i += 1
                    elif k and not v and i + 1 < len(parts):
                        # "Key:" with empty value → next part is the value
                        v = parts[i + 1].strip()
                        if v:
                            result.append((k, v, source))
                        i += 2
                    else:
                        i += 1
                elif i + 1 < len(parts) and ":" not in parts[i + 1]:
                    # Adjacent pair: neither part has a colon; treat as k → v
                    k, v = part, parts[i + 1]
                    if k and v:
                        result.append((k, v, source))
                    i += 2
                else:
                    i += 1
            return result


        for page in ast.pages:
            for block in page.text_blocks:
                text = block.text.strip()
                lines = text.split("\n")
                for line in lines:
                    parts = [p.strip() for p in line.split("\t") if p.strip()]
                    if len(parts) >= 2:
                        pairs.extend(_extract_from_parts(parts, f"TextBlock P{page.page_number}"))
                    elif len(parts) == 1 and ":" in parts[0]:
                        k, v = parts[0].split(":", 1)
                        if k.strip() and v.strip():
                            pairs.append((k.strip(), v.strip(), f"TextBlock P{page.page_number}"))

        if table:
            for row in table.rows:
                cells = [c.strip() for c in row.cells if c.strip()]
                for cell in cells:
                    if ":" in cell:
                        k, v = cell.split(":", 1)
                        if k.strip() and v.strip():
                            pairs.append((k.strip(), v.strip(), f"TableRow R{row.row_index}"))

        return pairs

    def _clean_field_value(self, val: str) -> str:
        cleaned = val.strip()
        for kw in self.TRUNCATION_KEYWORDS:
            match = re.search(r"\b" + kw + r"[\s#.:/-]", cleaned, re.IGNORECASE)
            if match and match.start() > 0:
                cleaned = cleaned[:match.start()].strip()
        return cleaned.strip()

    def _resolve_supplier(
        self, ast: DocumentAST, table: Optional[ExtractedTable], col_map: Dict[str, int], kv_pairs: List[Tuple[str, str, str]]
    ) -> str:
        # Priority 1: Repeated metadata column in the primary table
        if table and "supplier" in col_map:
            col_idx = col_map["supplier"]
            values = [r.cells[col_idx].strip() for r in table.rows if col_idx < len(r.cells) and r.cells[col_idx].strip()]
            if values and values[0].lower() not in self.INVALID_SUPPLIER_VALUES:
                return values[0]

        # Priority 2: Explicit key-value pairs (highest confidence)
        for k, v, src in kv_pairs:
            k_clean = k.lower().strip()
            for pat in self.SUPPLIER_KEY_PATTERNS:
                if re.search(pat, k_clean, re.IGNORECASE):
                    v_clean = self._clean_field_value(v)
                    v_clean = re.sub(r"^[:\s-]+", "", v_clean).strip()
                    if (
                        len(v_clean) >= 3
                        and v_clean.lower() not in self.INVALID_SUPPLIER_VALUES
                        and not self.DOCUMENT_TITLE_PATTERNS.search(v_clean)
                        and not self.LABEL_PREFIXES.search(v_clean)
                    ):
                        return v_clean

        # Priority 3: Fallback text-block scan (very conservative)
        # Only accept if:
        # - not a document title banner
        # - not starting with a label prefix keyword
        # - has at least 2 words
        # - at least one word is title-case and ≥4 chars (company name heuristic)
        # - does not look like a key-value line (no ":" mid-text with a short key)
        GENERIC_WORDS = {
            "ltd", "pvt", "private", "limited", "co", "corp", "inc",
            "industries", "industrial", "solutions", "enterprises", "trading",
            "services", "works", "technologies", "engineering"
        }

        for page in ast.pages:
            for block in page.text_blocks[:4]:
                # Only take the first tab-delimited segment (the leftmost cell)
                text = block.text.split("\t")[0].strip()
                if not text or len(text) < 4:
                    continue
                words = text.split()
                if len(words) > 10 or len(words) < 2:
                    continue
                if text.lower() in self.INVALID_SUPPLIER_VALUES:
                    continue
                if self.DOCUMENT_TITLE_PATTERNS.search(text):
                    continue
                if self.LABEL_PREFIXES.search(text):
                    continue
                # Reject if the text looks like a label line (short key + colon + value)
                if re.match(r"^[A-Za-z\s]{1,20}:\s*.+", text):
                    continue
                # Must have at least one plausible company-name word (title-case, ≥4 chars)
                # OR contain a known company-suffix word
                has_title_case = any(w[0].isupper() and len(w) >= 4 and w.lower() not in GENERIC_WORDS for w in words)
                has_company_suffix = any(w.lower() in GENERIC_WORDS for w in words)
                if has_title_case or has_company_suffix:
                    return text

        return "Unknown Supplier"


    def _resolve_quote_number(
        self, ast: DocumentAST, table: Optional[ExtractedTable], col_map: Dict[str, int], kv_pairs: List[Tuple[str, str, str]]
    ) -> Optional[str]:
        if table and "quote_number" in col_map:
            col_idx = col_map["quote_number"]
            values = [r.cells[col_idx].strip() for r in table.rows if col_idx < len(r.cells) and r.cells[col_idx].strip()]
            if values and len(values[0]) >= 3:
                return values[0]

        for k, v, src in kv_pairs:
            k_clean = k.lower().strip()
            for pat in self.QUOTE_NUM_KEY_PATTERNS:
                if re.search(pat, k_clean, re.IGNORECASE):
                    v_clean = self._clean_field_value(v)
                    v_clean = re.sub(r"^[:\s#-]+", "", v_clean).strip()
                    match = re.search(r"([A-Za-z0-9\-_/]+)", v_clean)
                    if match:
                        code = match.group(1).strip()
                        if code.lower() not in ["no", "number", "ref", "dated", "date"] and len(code) >= 3:
                            return code

        pattern = r"(?:quote\s*(?:no|number|ref|#)?|quotation\s*(?:no|number|ref|#)?|ref\s*(?:no|number|#)?)[\s#.:/\t-]*([A-Za-z0-9\-_/]+)"
        for page in ast.pages:
            for block in page.text_blocks:
                match = re.search(pattern, block.text, re.IGNORECASE)
                if match:
                    val = match.group(1).strip()
                    if val.lower() not in ["no", "number", "ref", "dated", "date"] and len(val) >= 3:
                        return val
        return None

    def _resolve_dates(
        self, ast: DocumentAST, table: Optional[ExtractedTable], col_map: Dict[str, int], kv_pairs: List[Tuple[str, str, str]]
    ) -> Tuple[Optional[date], Optional[date]]:
        quote_date = None
        valid_until = None

        if table and "quote_date" in col_map:
            col_idx = col_map["quote_date"]
            values = [r.cells[col_idx].strip() for r in table.rows if col_idx < len(r.cells) and r.cells[col_idx].strip()]
            if values:
                quote_date = parse_flexible_date(values[0])

        for k, v, src in kv_pairs:
            k_clean = k.lower().strip()
            for pat in self.DATE_KEY_PATTERNS:
                if re.search(pat, k_clean, re.IGNORECASE) and not quote_date:
                    v_clean = self._clean_field_value(v)
                    quote_date = parse_flexible_date(v_clean)

            for pat in self.VALIDITY_KEY_PATTERNS:
                if re.search(pat, k_clean, re.IGNORECASE) and not valid_until:
                    v_clean = self._clean_field_value(v)
                    valid_until = parse_flexible_date(v_clean)

        if not quote_date:
            date_patterns = [
                r"(?:quote\s*date|dated|date)[\s#.:/\t-]*(\d{1,4}[-/.][A-Za-z0-9]+[-/.]\d{2,4})",
                r"(\d{1,2}[-/.][A-Za-z0-9]+[-/.]\d{2,4})"
            ]
            for page in ast.pages:
                for block in page.text_blocks:
                    for pat in date_patterns:
                        match = re.search(pat, block.text, re.IGNORECASE)
                        if match and not quote_date:
                            quote_date = parse_flexible_date(match.group(1))

        return quote_date, valid_until

    def _resolve_currency(
        self, ast: DocumentAST, table: Optional[ExtractedTable], col_map: Dict[str, int], kv_pairs: List[Tuple[str, str, str]]
    ) -> Optional[str]:
        if table and "currency" in col_map:
            col_idx = col_map["currency"]
            values = [r.cells[col_idx].strip() for r in table.rows if col_idx < len(r.cells) and r.cells[col_idx].strip()]
            if values:
                norm = normalize_currency(values[0], default=None)
                if norm:
                    return norm

        for k, v, src in kv_pairs:
            k_clean = k.lower().strip()
            if "currency" in k_clean:
                v_clean = self._clean_field_value(v)
                norm = normalize_currency(v_clean, default=None)
                if norm:
                    return norm

        text = ast.full_text
        if "₹" in text or "INR" in text or "Rs." in text or "rs" in text.lower():
            return "INR"
        if "$" in text or "USD" in text:
            return "USD"
        if "€" in text or "EUR" in text:
            return "EUR"
        if "£" in text or "GBP" in text:
            return "GBP"
        return None

    def _resolve_commercial_terms(
        self, ast: DocumentAST, table: Optional[ExtractedTable], kv_pairs: List[Tuple[str, str, str]]
    ) -> Tuple[Optional[PaymentTerms], Optional[DeliveryTerms]]:
        payment_terms = None
        delivery_terms = None

        snippets_with_source: List[Tuple[str, Optional[Provenance]]] = []
        for p in ast.pages:
            for b in p.text_blocks:
                snippets_with_source.append((b.text, Provenance(page_number=p.page_number, source_text=b.text)))

        if table:
            for row in table.rows:
                for col_idx, cell in enumerate(row.cells):
                    if any(kw in cell.lower() for kw in ["payment", "delivery", "credit", "advance", "lead time", "dispatch"]):
                        prov = Provenance(
                            page_number=row.page_number,
                            sheet_name=row.sheet_name,
                            row_idx=row.row_index,
                            col_idx=col_idx,
                            source_text=cell
                        )
                        snippets_with_source.append((cell, prov))

        # 1. Payment Terms
        payment_keywords = ["payment", "credit", "terms of payment", "credit period", "payment condition", "net "]
        for text, prov in snippets_with_source:
            if any(kw in text.lower() for kw in payment_keywords) and not payment_terms:
                credit_match = re.search(r"(\d+)\s*(?:days|days\s*credit|days\s*net)", text, re.IGNORECASE)
                if not credit_match and re.search(r"\bnet\s*(\d+)\b", text, re.IGNORECASE):
                    credit_match = re.search(r"\bnet\s*(\d+)\b", text, re.IGNORECASE)
                credit_days = int(credit_match.group(1)) if credit_match else None

                adv_match = re.search(r"(\d+)%\s*(?:advance|upfront)", text, re.IGNORECASE)
                advance_pct = Decimal(adv_match.group(1)) if adv_match else None

                if credit_days is not None or advance_pct is not None or "payment" in text.lower():
                    payment_terms = PaymentTerms(
                        raw_text=text.strip(),
                        credit_days=credit_days,
                        advance_pct=advance_pct,
                        provenance=prov
                    )

        # 2. Delivery Terms
        delivery_keywords = ["delivery", "lead time", "dispatch", "exw", "fob", "cif", "expected delivery"]
        for text, prov in snippets_with_source:
            if any(kw in text.lower() for kw in delivery_keywords) and not delivery_terms:
                lead_match = re.search(r"(?:delivery|lead\s*time|dispatch)[\s#.:/-]*(\d+)\s*(?:days|weeks|working\s*days)", text, re.IGNORECASE)
                if not lead_match:
                    lead_match = re.search(r"(\d+)\s*(?:days|weeks)\s*(?:from\s*po|delivery)", text, re.IGNORECASE)
                
                lead_days = None
                if lead_match:
                    val = int(lead_match.group(1))
                    lead_days = val * 7 if "week" in lead_match.group(0).lower() else val

                incoterm = None
                for term in ["EXW", "FOB", "CIF", "DDP", "FOR"]:
                    if re.search(r"\b" + term + r"\b", text, re.IGNORECASE):
                        incoterm = term
                        break

                if lead_days is not None or incoterm is not None or "delivery" in text.lower() or "dispatch" in text.lower():
                    delivery_terms = DeliveryTerms(
                        incoterm=incoterm,
                        lead_time_days_default=lead_days,
                        raw_text=text.strip(),
                        provenance=prov
                    )

        return payment_terms, delivery_terms
