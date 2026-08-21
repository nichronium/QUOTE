"""
Multi-Signal Item Matcher Engine.
Implements deterministic identifier matching (Supplier SKU -> Manufacturer PN -> Internal SKU)
followed by RapidFuzz multi-signal fuzzy description scoring and dimension-safe UOM resolution.
INVARIANT: Original extracted QuoteItem data is NEVER mutated or overwritten.
"""

from decimal import Decimal
import re
from typing import Dict, List, Optional, Set, Tuple
from rapidfuzz import fuzz

from core.canonical_quote import CanonicalQuote, QuoteItem
from matching.models import (
    ItemMasterRecord,
    MatchCandidate,
    MatchedQuoteItem,
    MatchMethod,
    MatchStatus,
    RFQLineItem,
    UOMConversionResult,
)
from matching.uom_resolver import UOMResolver


class ItemMatcher:
    """Multi-signal enterprise item matcher with explainable downstream results."""

    HIGH_CONFIDENCE_THRESHOLD = 0.88
    REVIEW_THRESHOLD = 0.70
    CANDIDATE_MARGIN_THRESHOLD = 0.08

    def __init__(self):
        self.uom_resolver = UOMResolver()

    def match_quote(
        self,
        quote: CanonicalQuote,
        item_master: List[ItemMasterRecord],
        rfq_lines: Optional[List[RFQLineItem]] = None
    ) -> List[MatchedQuoteItem]:
        effective_master = [im for im in item_master if getattr(im, "status", "ACTIVE") != "INACTIVE"]
        if rfq_lines:
            existing_skus = {self._normalize_code(im.internal_sku) for im in effective_master if im.internal_sku}
            existing_ids = {im.internal_item_id for im in effective_master if im.internal_item_id}
            for line in rfq_lines:
                line_sku_norm = self._normalize_code(line.sku or line.rfq_line_id)
                line_id = line.internal_item_id or line.rfq_line_id
                if (line_sku_norm and line_sku_norm not in existing_skus) or (line_id and line_id not in existing_ids):
                    effective_master.append(ItemMasterRecord(
                        internal_item_id=line_id,
                        internal_sku=line.sku or line.rfq_line_id,
                        canonical_description=line.description,
                        stocking_uom=line.requested_uom
                    ))
                    if line_sku_norm:
                        existing_skus.add(line_sku_norm)
                    if line_id:
                        existing_ids.add(line_id)

        return [
            self.match_quote_item(item, effective_master, rfq_lines)
            for item in quote.items
        ]

    def match_quote_item(
        self,
        quote_item: QuoteItem,
        item_master: List[ItemMasterRecord],
        rfq_lines: Optional[List[RFQLineItem]] = None
    ) -> MatchedQuoteItem:
        """
        Executes multi-stage matching cascade:
        1. Exact Supplier SKU
        2. Exact Manufacturer PN
        3. Exact Internal SKU
        4. Multi-Signal RapidFuzz Description Scoring
        """
        review_reasons: List[str] = []
        matched_master: Optional[MatchCandidate] = None
        matched_rfq: Optional[MatchCandidate] = None
        top_candidates: List[MatchCandidate] = []

        quote_sku = self._normalize_code(quote_item.supplier_part_number)
        quote_desc = quote_item.raw_description.strip()

        # =========================================================================
        # STAGE 1: DETERMINISTIC EXACT IDENTIFIER MATCHING
        # =========================================================================
        if quote_sku:
            for rec in item_master:
                # A) Supplier SKU Exact Match
                approved_skus = { self._normalize_code(s) for s in rec.approved_supplier_part_numbers }
                if quote_sku in approved_skus:
                    matched_master = MatchCandidate(
                        candidate_item_id=rec.internal_item_id,
                        candidate_sku=rec.internal_sku,
                        candidate_description=rec.canonical_description,
                        match_method=MatchMethod.SUPPLIER_SKU_EXACT,
                        match_score=1.0,
                        match_status=MatchStatus.EXACT_MATCH,
                        explanations=[f"Exact match on approved supplier part number '{quote_item.supplier_part_number}'"],
                        matched_fields=["supplier_part_number"],
                        source_provenance=quote_item.provenance
                    )
                    break

                # B) Manufacturer Part Number Exact Match
                mfg_pn = self._normalize_code(rec.manufacturer_part_number)
                if mfg_pn and quote_sku == mfg_pn:
                    matched_master = MatchCandidate(
                        candidate_item_id=rec.internal_item_id,
                        candidate_sku=rec.internal_sku,
                        candidate_description=rec.canonical_description,
                        match_method=MatchMethod.MANUFACTURER_PN_EXACT,
                        match_score=1.0,
                        match_status=MatchStatus.EXACT_MATCH,
                        explanations=[f"Exact match on manufacturer part number '{rec.manufacturer_part_number}'"],
                        matched_fields=["manufacturer_part_number"],
                        source_provenance=quote_item.provenance
                    )
                    break

                # C) Internal SKU Exact Match
                int_sku = self._normalize_code(rec.internal_sku)
                if int_sku and quote_sku == int_sku:
                    matched_master = MatchCandidate(
                        candidate_item_id=rec.internal_item_id,
                        candidate_sku=rec.internal_sku,
                        candidate_description=rec.canonical_description,
                        match_method=MatchMethod.INTERNAL_SKU_EXACT,
                        match_score=1.0,
                        match_status=MatchStatus.EXACT_MATCH,
                        explanations=[f"Exact match on internal SKU '{rec.internal_sku}'"],
                        matched_fields=["internal_sku"],
                        source_provenance=quote_item.provenance
                    )
                    break

        # =========================================================================
        # STAGE 2: FUZZY MULTI-SIGNAL DESCRIPTION MATCHING
        # =========================================================================
        if not matched_master:
            scored_candidates: List[Tuple[float, ItemMasterRecord, List[str]]] = []

            for rec in item_master:
                score, explanations = self._compute_multi_signal_score(quote_item, rec)
                scored_candidates.append((score, rec, explanations))

            # Sort candidates descending by score
            scored_candidates.sort(key=lambda x: x[0], reverse=True)

            for score, rec, expls in scored_candidates[:5]:
                st = MatchStatus.HIGH_CONFIDENCE_MATCH if score >= self.HIGH_CONFIDENCE_THRESHOLD else (
                    MatchStatus.REVIEW_REQUIRED if score >= self.REVIEW_THRESHOLD else MatchStatus.UNMATCHED
                )
                top_candidates.append(MatchCandidate(
                    candidate_item_id=rec.internal_item_id,
                    candidate_sku=rec.internal_sku,
                    candidate_description=rec.canonical_description,
                    match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                    match_score=score,
                    match_status=st,
                    explanations=expls,
                    matched_fields=["canonical_description"],
                    source_provenance=quote_item.provenance
                ))

            if top_candidates:
                best_cand = top_candidates[0]
                second_score = top_candidates[1].match_score if len(top_candidates) > 1 else 0.0

                # Ambiguity guard: If top 2 candidates are too close, flag REVIEW_REQUIRED
                if len(top_candidates) > 1 and (best_cand.match_score - second_score) < self.CANDIDATE_MARGIN_THRESHOLD and best_cand.match_score >= self.REVIEW_THRESHOLD:
                    best_cand.match_status = MatchStatus.REVIEW_REQUIRED
                    review_reasons.append(f"Ambiguity: Close competing candidate '{top_candidates[1].candidate_sku}' (score: {second_score:.2f})")

                if best_cand.match_score >= self.REVIEW_THRESHOLD:
                    matched_master = best_cand
                else:
                    review_reasons.append(f"No credible candidate above review threshold (score: {best_cand.match_score:.2f} < {self.REVIEW_THRESHOLD:.2f})")

        # =========================================================================
        # STAGE 3: UOM COMPATIBILITY & CONVERSION RESOLUTION
        # =========================================================================
        uom_result: Optional[UOMConversionResult] = None
        item_master_rec: Optional[ItemMasterRecord] = None

        if matched_master:
            for rec in item_master:
                if rec.internal_item_id == matched_master.candidate_item_id:
                    item_master_rec = rec
                    break

        if item_master_rec:
            uom_result = self.uom_resolver.resolve_uom_conversion(
                source_uom=quote_item.quoted_uom,
                target_uom=item_master_rec.stocking_uom,
                quoted_quantity=quote_item.quoted_qty,
                item_master=item_master_rec
            )

            if not uom_result.is_compatible:
                review_reasons.append(uom_result.error_reason or "Incompatible UOM")

        # Final Status Determination
        if uom_result and not uom_result.is_compatible:
            final_status = MatchStatus.UOM_INCOMPATIBLE
        elif matched_master:
            final_status = matched_master.match_status
        else:
            final_status = MatchStatus.UNMATCHED

        # Resolve matched_rfq
        if matched_master and rfq_lines:
            for rline in rfq_lines:
                if (rline.sku and self._normalize_code(rline.sku) == self._normalize_code(matched_master.candidate_sku)) or \
                   (rline.internal_item_id and rline.internal_item_id == matched_master.candidate_item_id) or \
                   (rline.rfq_line_id == matched_master.candidate_item_id):
                    matched_rfq = MatchCandidate(
                        candidate_item_id=rline.rfq_line_id,
                        candidate_sku=rline.sku or rline.rfq_line_id,
                        candidate_description=rline.description,
                        match_method=matched_master.match_method,
                        match_score=matched_master.match_score,
                        match_status=matched_master.match_status,
                        explanations=matched_master.explanations,
                        matched_fields=matched_master.matched_fields,
                        source_provenance=quote_item.provenance
                    )
                    break

        return MatchedQuoteItem(
            quote_item=quote_item,             # Unchanged original data
            item_master_match=matched_master,
            rfq_match=matched_rfq,
            uom_conversion=uom_result,
            match_status=final_status,
            review_reasons=review_reasons,
            top_candidates=top_candidates
        )

    def _compute_multi_signal_score(
        self, quote_item: QuoteItem, rec: ItemMasterRecord
    ) -> Tuple[float, List[str]]:
        """Computes weighted multi-signal fuzzy score with spec conflict penalties."""
        q_raw = quote_item.raw_description.lower()
        c_raw = rec.canonical_description.lower()
        explanations = []

        q_desc = self._normalize_desc(q_raw)
        c_desc = self._normalize_desc(c_raw)

        # 1. Token Sort Ratio (40%)
        ts_ratio = fuzz.token_sort_ratio(q_desc, c_desc) / 100.0

        # 2. Token Set / Jaccard Overlap (25%)
        q_tokens = set(re.findall(r"\b\w+\b", q_desc))
        c_tokens = set(re.findall(r"\b\w+\b", c_desc))
        intersection = q_tokens & c_tokens
        union = q_tokens | c_tokens
        jaccard = len(intersection) / len(union) if union else 0.0

        # 3. Specification Tokens (Size, Dimensions, Model, Voltage, Grade) (20%)
        q_specs = self._extract_spec_tokens(q_raw)
        c_specs = self._extract_spec_tokens(c_raw)

        spec_score = 0.5
        if q_specs and c_specs:
            if q_specs == c_specs or q_specs.issubset(c_specs) or c_specs.issubset(q_specs):
                spec_score = 1.0
                explanations.append(f"Specification agreement: {', '.join(q_specs)}")
            elif all(any(qs in cs or cs in qs for cs in c_specs) for qs in q_specs):
                # Compatible partial specifications (e.g. M8 in M8x40mm)
                spec_score = 0.85
                explanations.append(f"Compatible partial specifications: {q_specs} ~ {c_specs}")
            else:
                # Direct spec conflict (e.g. 40mm vs 50mm)
                spec_score = 0.0
                explanations.append(f"Specification conflict: {q_specs} vs {c_specs}")

        # 4. Brand / Manufacturer Agreement (15%)
        brand_score = 0.5
        if rec.brand:
            if rec.brand.lower() in q_raw:
                brand_score = 1.0
                explanations.append(f"Brand agreement: {rec.brand}")

        # Composite Weighted Score
        composite = (0.40 * ts_ratio) + (0.25 * jaccard) + (0.20 * spec_score) + (0.15 * brand_score)

        # If specs conflict, cap composite score to prevent false positives
        if q_specs and c_specs and spec_score == 0.0:
            composite = min(composite, 0.65)

        explanations.append(f"Token sort ratio: {ts_ratio:.2f}, Jaccard overlap: {jaccard:.2f}")
        return round(composite, 3), explanations

    def _normalize_desc(self, text: str) -> str:
        text_norm = text.lower()
        text_norm = re.sub(r"stainless\s*steel\s*(?:grade\s*)?304", "ss304", text_norm)
        text_norm = re.sub(r"stainless\s*steel\s*(?:grade\s*)?316", "ss316", text_norm)
        text_norm = re.sub(r"deep\s*groove\s*ball\s*bearing", "ball bearing", text_norm)
        return text_norm


    def _extract_spec_tokens(self, text: str) -> Set[str]:
        text_norm = text.lower()
        text_norm = re.sub(r"stainless\s*steel\s*(?:grade\s*)?304", "ss304", text_norm)
        text_norm = re.sub(r"stainless\s*steel\s*(?:grade\s*)?316", "ss316", text_norm)
        pat = r"\b(?:\d+(?:x\d+)+(?:mm|cm|m)?|m\d+(?:x\d+)+(?:mm)?|m\d+|\d+(?:\.\d+)?(?:mm|cm|mtr|m|kg|kw|v|vac|vdc|w|inch|in|dn\d+|sq\.mm)|ss\d+|grade\s+[a-z0-9]+)\b"
        return set(re.findall(pat, text_norm))


    def _normalize_code(self, code: Optional[str]) -> Optional[str]:
        if not code:
            return None
        return re.sub(r"[^A-Z0-9]", "", code.upper().strip())
