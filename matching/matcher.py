"""
Multi-Signal Universal Item Matcher Engine.
Implements domain-agnostic, multi-signal evidence evaluation:
1. Historical Human Confirmed Supplier-to-Item Memory Mappings
2. Deterministic Identifier Matching (Supplier SKU -> Manufacturer PN -> Internal SKU)
3. Multi-Signal RapidFuzz Description & Specification Conflict Scoring
4. Hard Conflict Gatekeeper (overrides raw confidence score)
5. Structured Decision-Band Classification & Explainable Evidence Checklists
INVARIANT: Original extracted QuoteItem data is NEVER mutated or overwritten.
"""

from decimal import Decimal
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from rapidfuzz import fuzz

from core.canonical_quote import CanonicalQuote, QuoteItem
from matching.models import (
    DecisionBand,
    EvidenceItem,
    ItemMasterRecord,
    MatchCandidate,
    MatchedQuoteItem,
    MatchMethod,
    MatchStatus,
    RFQLineItem,
    SupplierMappingRecord,
    UOMConversionResult,
)
from matching.uom_resolver import UOMResolver


class ItemMatcher:
    """Multi-signal universal item matcher with explainable downstream results."""

    HIGH_CONFIDENCE_THRESHOLD: float = 0.95
    BULK_CANDIDATE_THRESHOLD: float = 0.80
    MANUAL_REVIEW_THRESHOLD: float = 0.45
    CANDIDATE_MARGIN_THRESHOLD: float = 0.086

    def __init__(self):
        self.uom_resolver = UOMResolver()

    def match_quote(
        self,
        quote: CanonicalQuote,
        item_master: List[ItemMasterRecord],
        rfq_lines: Optional[List[RFQLineItem]] = None,
        supplier_id: Optional[str] = None,
        supplier_mappings: Optional[List[SupplierMappingRecord]] = None
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
                        category=line.category,
                        manufacturer=line.manufacturer,
                        manufacturer_part_number=line.manufacturer_part_number,
                        canonical_description=line.description,
                        stocking_uom=line.requested_uom or "PCS",
                        specifications=line.specifications
                    ))
                    if line_sku_norm:
                        existing_skus.add(line_sku_norm)
                    if line_id:
                        existing_ids.add(line_id)

        supp_id = supplier_id or getattr(quote, "supplier_id", None)

        return [
            self.match_quote_item(
                item,
                effective_master,
                rfq_lines,
                supplier_id=supp_id,
                supplier_mappings=supplier_mappings
            )
            for item in quote.items
        ]

    def match_quote_item(
        self,
        quote_item: QuoteItem,
        item_master: List[ItemMasterRecord],
        rfq_lines: Optional[List[RFQLineItem]] = None,
        supplier_id: Optional[str] = None,
        supplier_mappings: Optional[List[SupplierMappingRecord]] = None
    ) -> MatchedQuoteItem:
        """
        Executes universal multi-stage matching cascade:
        1. Historical Human Decision Memory
        2. Exact Supplier SKU / Part Number
        3. Exact Manufacturer Part Number
        4. Exact Internal SKU
        5. Multi-Signal Category-Agnostic Description & Specification Matching
        6. Hard Conflict Detection & Decision Band Classification
        7. Dimension-Safe UOM Resolution
        """
        review_reasons: List[str] = []
        matched_master: Optional[MatchCandidate] = None
        matched_rfq: Optional[MatchCandidate] = None
        top_candidates: List[MatchCandidate] = []
        has_hard_conflict = False
        conflict_reasons: List[str] = []

        quote_sku = self._normalize_code(quote_item.supplier_part_number)
        quote_desc = quote_item.raw_description.strip()

        # =========================================================================
        # STAGE 0: HISTORICAL HUMAN CONFIRMED SUPPLIER-ITEM MEMORY
        # =========================================================================
        if supplier_id and quote_sku and supplier_mappings:
            for sm in supplier_mappings:
                if self._normalize_code(sm.supplier_id) == self._normalize_code(supplier_id) and \
                   self._normalize_code(sm.supplier_part_number) == quote_sku:
                    target_rec = next((r for r in item_master if self._normalize_code(r.internal_sku) == self._normalize_code(sm.internal_sku) or r.internal_item_id == sm.internal_item_id), None)
                    if target_rec:
                        # Validate that new quotation line does not have contradictory specifications
                        q_specs = self._extract_typed_specs(quote_item.raw_description)
                        c_spec_text = target_rec.canonical_description
                        if target_rec.specifications:
                            c_spec_text += " " + " ".join(f"{k} {v}" for k, v in target_rec.specifications.items())
                        c_specs = self._extract_typed_specs(c_spec_text)

                        shared_types = set(q_specs.keys()) & set(c_specs.keys())
                        spec_conflicts = [f"{st} conflict: '{q_specs[st]}' vs '{c_specs[st]}'" for st in shared_types if q_specs[st] != c_specs[st]]

                        if spec_conflicts:
                            has_hard_conflict = True
                            conflict_reasons.extend(spec_conflicts)
                            ev_checklist = [
                                EvidenceItem(signal="HISTORICAL_MEMORY", status="WARNING", description=f"Known mapping for '{sm.supplier_part_number}' -> '{sm.internal_sku}' contradicted by current item specifications"),
                                EvidenceItem(signal="SPEC_CONFLICT", status="FAIL", description="; ".join(spec_conflicts))
                            ]
                            matched_master = MatchCandidate(
                                candidate_item_id=target_rec.internal_item_id,
                                candidate_sku=target_rec.internal_sku,
                                candidate_description=target_rec.canonical_description,
                                match_method=MatchMethod.HISTORICAL_SUPPLIER_MAPPING,
                                match_score=0.60,
                                match_status=MatchStatus.REVIEW_REQUIRED,
                                decision_band=DecisionBand.BLOCKED_CONFLICT,
                                explanations=[f"Historical mapping blocked by specification conflict: {', '.join(spec_conflicts)}"],
                                evidence_checklist=ev_checklist,
                                matched_fields=["supplier_part_number", "historical_mapping"],
                                has_hard_conflict=True,
                                conflict_reasons=spec_conflicts,
                                source_provenance=quote_item.provenance
                            )
                        else:
                            ev_checklist = [
                                EvidenceItem(signal="HISTORICAL_MEMORY", status="PASS", description=f"Historically confirmed by {sm.confirmed_by} on {sm.confirmed_at[:10]}"),
                                EvidenceItem(signal="SUPPLIER_PN", status="PASS", description=f"Known supplier mapping for '{sm.supplier_part_number}' -> '{sm.internal_sku}'"),
                                EvidenceItem(signal="DESCRIPTION", status="INFO", description=f"Aligned to catalog item: {target_rec.canonical_description}")
                            ]
                            matched_master = MatchCandidate(
                                candidate_item_id=target_rec.internal_item_id,
                                candidate_sku=target_rec.internal_sku,
                                candidate_description=target_rec.canonical_description,
                                match_method=MatchMethod.HISTORICAL_SUPPLIER_MAPPING,
                                match_score=0.98,
                                match_status=MatchStatus.EXACT_MATCH,
                                decision_band=DecisionBand.HIGH_CONFIDENCE,
                                explanations=[f"Reused historical human confirmed mapping: Supplier Part '{sm.supplier_part_number}' -> Internal SKU '{sm.internal_sku}'"],
                                evidence_checklist=ev_checklist,
                                matched_fields=["supplier_part_number", "historical_mapping"],
                                source_provenance=quote_item.provenance
                            )
                        break

        # =========================================================================
        # STAGE 1: DETERMINISTIC EXACT IDENTIFIER MATCHING
        # =========================================================================
        if not matched_master and quote_sku:
            for rec in item_master:
                # A) Supplier SKU Exact Match
                approved_skus = { self._normalize_code(s) for s in rec.approved_supplier_part_numbers }
                if quote_sku in approved_skus:
                    ev_checklist = [
                        EvidenceItem(signal="SUPPLIER_PN", status="PASS", description=f"Exact match on approved supplier part number '{quote_item.supplier_part_number}'"),
                        EvidenceItem(signal="INTERNAL_SKU", status="PASS", description=f"Maps to internal SKU '{rec.internal_sku}'"),
                        EvidenceItem(signal="DESCRIPTION", status="INFO", description=f"Catalog description: {rec.canonical_description}")
                    ]
                    matched_master = MatchCandidate(
                        candidate_item_id=rec.internal_item_id,
                        candidate_sku=rec.internal_sku,
                        candidate_description=rec.canonical_description,
                        match_method=MatchMethod.SUPPLIER_SKU_EXACT,
                        match_score=1.0,
                        match_status=MatchStatus.EXACT_MATCH,
                        decision_band=DecisionBand.HIGH_CONFIDENCE,
                        explanations=[f"Exact match on approved supplier part number '{quote_item.supplier_part_number}'"],
                        evidence_checklist=ev_checklist,
                        matched_fields=["supplier_part_number"],
                        source_provenance=quote_item.provenance
                    )
                    break

                # B) Manufacturer Part Number Exact Match
                mfg_pn = self._normalize_code(rec.manufacturer_part_number)
                if mfg_pn and quote_sku == mfg_pn:
                    ev_checklist = [
                        EvidenceItem(signal="MANUFACTURER_PN", status="PASS", description=f"Exact match on manufacturer part number '{rec.manufacturer_part_number}'"),
                        EvidenceItem(signal="INTERNAL_SKU", status="PASS", description=f"Maps to internal SKU '{rec.internal_sku}'")
                    ]
                    matched_master = MatchCandidate(
                        candidate_item_id=rec.internal_item_id,
                        candidate_sku=rec.internal_sku,
                        candidate_description=rec.canonical_description,
                        match_method=MatchMethod.MANUFACTURER_PN_EXACT,
                        match_score=1.0,
                        match_status=MatchStatus.EXACT_MATCH,
                        decision_band=DecisionBand.HIGH_CONFIDENCE,
                        explanations=[f"Exact match on manufacturer part number '{rec.manufacturer_part_number}'"],
                        evidence_checklist=ev_checklist,
                        matched_fields=["manufacturer_part_number"],
                        source_provenance=quote_item.provenance
                    )
                    break

                # C) Internal SKU Exact Match
                int_sku = self._normalize_code(rec.internal_sku)
                if int_sku and quote_sku == int_sku:
                    ev_checklist = [
                        EvidenceItem(signal="INTERNAL_SKU", status="PASS", description=f"Exact match on internal SKU '{rec.internal_sku}'"),
                        EvidenceItem(signal="DESCRIPTION", status="INFO", description=f"Catalog description: {rec.canonical_description}")
                    ]
                    matched_master = MatchCandidate(
                        candidate_item_id=rec.internal_item_id,
                        candidate_sku=rec.internal_sku,
                        candidate_description=rec.canonical_description,
                        match_method=MatchMethod.INTERNAL_SKU_EXACT,
                        match_score=1.0,
                        match_status=MatchStatus.EXACT_MATCH,
                        decision_band=DecisionBand.HIGH_CONFIDENCE,
                        explanations=[f"Exact match on internal SKU '{rec.internal_sku}'"],
                        evidence_checklist=ev_checklist,
                        matched_fields=["internal_sku"],
                        source_provenance=quote_item.provenance
                    )
                    break

        # =========================================================================
        # STAGE 2: FUZZY MULTI-SIGNAL DESCRIPTION & SPECIFICATION MATCHING
        # =========================================================================
        if not matched_master:
            scored_candidates: List[Tuple[float, ItemMasterRecord, List[str], List[EvidenceItem], bool, List[str]]] = []

            for rec in item_master:
                score, expls, ev_list, is_conflict, c_reasons = self._compute_multi_signal_score(
                    quote_item, rec, supplier_id=supplier_id
                )
                scored_candidates.append((score, rec, expls, ev_list, is_conflict, c_reasons))

            # Sort candidates descending by score
            scored_candidates.sort(key=lambda x: x[0], reverse=True)

            for score, rec, expls, ev_list, is_conflict, c_reasons in scored_candidates[:5]:
                # Assign Decision Band based on score and conflict
                if is_conflict:
                    band = DecisionBand.BLOCKED_CONFLICT
                    st = MatchStatus.REVIEW_REQUIRED
                elif score >= self.HIGH_CONFIDENCE_THRESHOLD:
                    band = DecisionBand.HIGH_CONFIDENCE
                    st = MatchStatus.HIGH_CONFIDENCE_MATCH
                elif score >= self.BULK_CANDIDATE_THRESHOLD:
                    band = DecisionBand.BULK_CANDIDATE
                    st = MatchStatus.REVIEW_REQUIRED
                elif score >= self.MANUAL_REVIEW_THRESHOLD:
                    band = DecisionBand.MANUAL_REVIEW
                    st = MatchStatus.REVIEW_REQUIRED
                else:
                    band = DecisionBand.LOW_CONFIDENCE
                    st = MatchStatus.UNMATCHED

                top_candidates.append(MatchCandidate(
                    candidate_item_id=rec.internal_item_id,
                    candidate_sku=rec.internal_sku,
                    candidate_description=rec.canonical_description,
                    match_method=MatchMethod.FUZZY_DESCRIPTION_MULTI_SIGNAL,
                    match_score=score,
                    match_status=st,
                    decision_band=band,
                    explanations=expls,
                    evidence_checklist=ev_list,
                    matched_fields=["canonical_description"],
                    has_hard_conflict=is_conflict,
                    conflict_reasons=c_reasons,
                    source_provenance=quote_item.provenance
                ))

            if top_candidates:
                best_cand = top_candidates[0]
                second_score = top_candidates[1].match_score if len(top_candidates) > 1 else 0.0

                has_hard_conflict = best_cand.has_hard_conflict
                conflict_reasons = best_cand.conflict_reasons.copy()

                # Ambiguity guard: If top 2 candidates are too close, downgrade from auto-resolved
                if len(top_candidates) > 1 and (best_cand.match_score - second_score) < self.CANDIDATE_MARGIN_THRESHOLD and best_cand.match_score >= self.BULK_CANDIDATE_THRESHOLD:
                    best_cand.match_status = MatchStatus.REVIEW_REQUIRED
                    best_cand.decision_band = DecisionBand.MANUAL_REVIEW
                    review_reasons.append(f"Ambiguity: Close competing candidate '{top_candidates[1].candidate_sku}' (score: {second_score:.2f})")
                    best_cand.evidence_checklist.append(EvidenceItem(
                        signal="AMBIGUITY",
                        status="WARNING",
                        description=f"Close competing candidate '{top_candidates[1].candidate_sku}' ({second_score * 100:.0f}%)"
                    ))

                if has_hard_conflict:
                    best_cand.decision_band = DecisionBand.BLOCKED_CONFLICT
                    best_cand.match_status = MatchStatus.REVIEW_REQUIRED
                    review_reasons.extend(conflict_reasons)
                    matched_master = best_cand
                elif best_cand.match_score >= self.MANUAL_REVIEW_THRESHOLD:
                    matched_master = best_cand
                else:
                    matched_master = None
                    has_hard_conflict = False
                    conflict_reasons = []
                    review_reasons.append(f"No credible candidate above review threshold (score: {best_cand.match_score:.2f} < {self.MANUAL_REVIEW_THRESHOLD:.2f})")

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
                uom_err = uom_result.error_reason or f"Incompatible UOM conversion between '{quote_item.quoted_uom}' and '{item_master_rec.stocking_uom}'"
                review_reasons.append(uom_err)
                has_hard_conflict = True
                conflict_reasons.append(uom_err)
                if matched_master:
                    matched_master.has_hard_conflict = True
                    matched_master.conflict_reasons.append(uom_err)
                    matched_master.decision_band = DecisionBand.BLOCKED_CONFLICT
                    matched_master.evidence_checklist.append(EvidenceItem(
                        signal="UOM",
                        status="FAIL",
                        description=f"Incompatible UOM: '{quote_item.quoted_uom}' vs '{item_master_rec.stocking_uom}'"
                    ))
            else:
                if matched_master:
                    matched_master.evidence_checklist.append(EvidenceItem(
                        signal="UOM",
                        status="PASS",
                        description=f"UOM Compatible ({uom_result.source_uom} -> {uom_result.target_uom})"
                    ))

        # Final Status & Decision Band Determination
        final_band: str = DecisionBand.LOW_CONFIDENCE
        if matched_master and has_hard_conflict:
            final_status = MatchStatus.UOM_INCOMPATIBLE if (uom_result and not uom_result.is_compatible) else MatchStatus.REVIEW_REQUIRED
            final_band = DecisionBand.BLOCKED_CONFLICT
        elif matched_master:
            final_status = matched_master.match_status
            final_band = matched_master.decision_band or (
                DecisionBand.HIGH_CONFIDENCE if matched_master.match_score >= self.HIGH_CONFIDENCE_THRESHOLD else (
                    DecisionBand.BULK_CANDIDATE if matched_master.match_score >= self.BULK_CANDIDATE_THRESHOLD else (
                        DecisionBand.MANUAL_REVIEW if matched_master.match_score >= self.MANUAL_REVIEW_THRESHOLD else DecisionBand.LOW_CONFIDENCE
                    )
                )
            )
        else:
            final_status = MatchStatus.UNMATCHED
            final_band = DecisionBand.LOW_CONFIDENCE

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
                        decision_band=final_band,
                        explanations=matched_master.explanations,
                        evidence_checklist=matched_master.evidence_checklist,
                        matched_fields=matched_master.matched_fields,
                        has_hard_conflict=has_hard_conflict,
                        conflict_reasons=conflict_reasons,
                        source_provenance=quote_item.provenance
                    )
                    break

        evidence_list = matched_master.evidence_checklist if matched_master else [
            EvidenceItem(signal="MATCHING", status="INFO", description="No confident catalog item identified")
        ]

        return MatchedQuoteItem(
            quote_item=quote_item,             # Unchanged original data
            item_master_match=matched_master,
            rfq_match=matched_rfq,
            uom_conversion=uom_result,
            match_status=final_status,
            decision_band=final_band,
            has_hard_conflict=has_hard_conflict,
            conflict_reasons=conflict_reasons,
            evidence_checklist=evidence_list,
            review_reasons=review_reasons,
            top_candidates=top_candidates
        )

    def _compute_multi_signal_score(
        self, quote_item: QuoteItem, rec: ItemMasterRecord, supplier_id: Optional[str] = None
    ) -> Tuple[float, List[str], List[EvidenceItem], bool, List[str]]:
        """
        Computes weighted multi-signal fuzzy score with deterministic conflict detection.
        Returns: (composite_score, explanations, evidence_checklist, is_conflict, conflict_reasons)
        """
        q_raw = quote_item.raw_description.lower()
        c_raw = rec.canonical_description.lower()
        explanations: List[str] = []
        evidence: List[EvidenceItem] = []
        is_conflict = False
        conflict_reasons: List[str] = []

        q_desc = self._normalize_desc(q_raw)
        c_desc = self._normalize_desc(c_raw)

        # 1. Token Sort Ratio (35%)
        ts_ratio = fuzz.token_sort_ratio(q_desc, c_desc) / 100.0

        # 2. Token Set / Jaccard Overlap (25%)
        q_tokens = set(re.findall(r"\b\w+\b", q_desc))
        c_tokens = set(re.findall(r"\b\w+\b", c_desc))
        intersection = q_tokens & c_tokens
        union = q_tokens | c_tokens
        jaccard = len(intersection) / len(union) if union else 0.0

        if ts_ratio >= 0.80 or jaccard >= 0.70:
            evidence.append(EvidenceItem(signal="DESCRIPTION", status="PASS", description=f"High description alignment ({ts_ratio * 100:.0f}%)"))
        elif ts_ratio >= 0.50:
            evidence.append(EvidenceItem(signal="DESCRIPTION", status="WARNING", description=f"Moderate description similarity ({ts_ratio * 100:.0f}%)"))
        else:
            evidence.append(EvidenceItem(signal="DESCRIPTION", status="FAIL", description=f"Low description similarity ({ts_ratio * 100:.0f}%)"))

        # 3. Universal Attribute & Specification Conflict Analysis (25%)
        q_specs = self._extract_typed_specs(q_raw)
        c_spec_text = c_raw
        if rec.specifications:
            c_spec_text += " " + " ".join(f"{k} {v}" for k, v in rec.specifications.items())
        c_specs = self._extract_typed_specs(c_spec_text)

        spec_score = 0.5
        spec_conflicts = []
        spec_agreements = []

        # Compare typed specifications (e.g. sqmm, cores, thread, length, grade, suffix)
        shared_types = set(q_specs.keys()) & set(c_specs.keys())
        if shared_types:
            for stype in shared_types:
                q_val = q_specs[stype]
                c_val = c_specs[stype]
                if q_val == c_val:
                    spec_agreements.append(f"{stype}: {q_val}")
                else:
                    spec_conflicts.append(f"{stype} conflict: '{q_val}' vs '{c_val}'")

            if spec_conflicts:
                spec_score = 0.0
                is_conflict = True
                for sc in spec_conflicts:
                    conflict_reasons.append(sc)
                    explanations.append(sc)
                    evidence.append(EvidenceItem(signal="SPECIFICATIONS", status="FAIL", description=sc))
            else:
                # If only a generic dimension matched and token overlap is low, keep spec score neutral
                if len(shared_types) == 1 and "dimension_mm" in shared_types and jaccard < 0.25:
                    spec_score = 0.5
                else:
                    spec_score = 1.0
                explanations.append(f"Specification agreement: {', '.join(spec_agreements)}")
                evidence.append(EvidenceItem(signal="SPECIFICATIONS", status="PASS", description=f"Specs match: {', '.join(spec_agreements)}"))
        elif q_specs or c_specs:
            spec_score = 0.6
            evidence.append(EvidenceItem(signal="SPECIFICATIONS", status="INFO", description="Partial specification metadata available"))

        # 4. Brand / Manufacturer / Category Agreement (15%)
        brand_score = 0.5
        brand_val = rec.brand or rec.manufacturer
        if brand_val:
            brand_lower = brand_val.lower()
            if brand_lower in q_raw or (supplier_id and brand_lower in supplier_id.lower()):
                brand_score = 1.0
                explanations.append(f"Brand/Manufacturer aligned: {brand_val}")
                evidence.append(EvidenceItem(signal="BRAND", status="PASS", description=f"Brand/Manufacturer aligned: {brand_val}"))
            else:
                evidence.append(EvidenceItem(signal="BRAND", status="INFO", description=f"Brand: {brand_val}"))

        # 5. Check Explicit Manufacturer Part Number Conflict
        q_mpn = quote_item.manufacturer_part_number
        if q_mpn and rec.manufacturer_part_number and (ts_ratio >= 0.50 or jaccard >= 0.30):
            norm_q_pn = self._normalize_code(q_mpn)
            norm_c_mpn = self._normalize_code(rec.manufacturer_part_number)
            if norm_q_pn and norm_c_mpn and norm_q_pn != norm_c_mpn and len(norm_q_pn) >= 4 and len(norm_c_mpn) >= 4:
                # If MPNs differ significantly on similar products, flag conflict
                if norm_q_pn not in norm_c_mpn and norm_c_mpn not in norm_q_pn:
                    is_conflict = True
                    conflict_msg = f"Manufacturer Part Number clash: '{q_mpn}' vs '{rec.manufacturer_part_number}'"
                    conflict_reasons.append(conflict_msg)
                    evidence.append(EvidenceItem(signal="MANUFACTURER_PN", status="FAIL", description=conflict_msg))

        # Composite Weighted Score
        composite = (0.35 * ts_ratio) + (0.25 * jaccard) + (0.25 * spec_score) + (0.15 * brand_score)

        # Multi-signal synergy boost: When description, specifications, and brand all strongly align with zero conflict
        if ts_ratio >= 0.95 and jaccard >= 0.95 and spec_score >= 0.85 and not is_conflict:
            composite = 1.0
        elif ts_ratio >= 0.70 and spec_score == 1.0 and brand_score == 1.0 and not is_conflict:
            composite = min(1.0, composite + 0.12)

        # Hard conflict penalty: If hard conflict detected, cap composite score to prevent false auto-acceptance
        if is_conflict:
            composite = min(composite, 0.65)

        explanations.append(f"Token sort: {ts_ratio:.2f}, Jaccard: {jaccard:.2f}")
        return round(composite, 3), explanations, evidence, is_conflict, conflict_reasons

    def _normalize_desc(self, text: str) -> str:
        text_norm = text.lower()
        text_norm = re.sub(r"stainless\s*steel\s*(?:grade\s*)?304", "ss304", text_norm)
        text_norm = re.sub(r"stainless\s*steel\s*(?:grade\s*)?316", "ss316", text_norm)
        text_norm = re.sub(r"\bss\s*304\b", "ss304", text_norm)
        text_norm = re.sub(r"\bss\s*316\b", "ss316", text_norm)
        text_norm = re.sub(r"deep\s*groove\s*ball\s*bearing", "ball bearing", text_norm)
        text_norm = re.sub(r"copper\s*flexible\s*cable", "cable", text_norm)
        text_norm = re.sub(r"\bannual\s*maintenance\s*contract\b", "amc", text_norm)
        text_norm = re.sub(r"\b1\s*(?:year|yr)\b", "annual", text_norm)
        text_norm = re.sub(r"\b(?:hex\s*)?allen\s*(?:key|wrench)\b", "hex key wrench", text_norm)
        text_norm = re.sub(r"\bhex\s*key\b", "hex key wrench", text_norm)
        text_norm = re.sub(r"\bpiece\b", "pc", text_norm)
        text_norm = re.sub(r"(\d+)\.0\s*mm", r"\1mm", text_norm)
        return text_norm

    def _extract_typed_specs(self, text: str) -> Dict[str, str]:
        """Extracts typed, canonicalized specifications from raw text."""
        text_norm = text.lower()
        specs: Dict[str, str] = {}

        # 1. Wire Gauge (sq.mm / sqmm)
        sq_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:sq\.?\s*mm|sqmm)", text_norm)
        if sq_match:
            val = float(sq_match.group(1))
            specs["wire_gauge"] = f"{int(val) if val.is_integer() else val}sqmm"

        # 2. Core Count (4 core, 4-core, 4C)
        core_match = re.search(r"(\d+)\s*(?:-| )?(?:core|c\b)", text_norm)
        if core_match:
            specs["core_count"] = f"{core_match.group(1)}core"

        # 3. Fastener Thread (M6, M8, M10, M12)
        thread_match = re.search(r"\b(m\d+)\b", text_norm)
        if thread_match:
            specs["thread_size"] = thread_match.group(1).upper()

        # 4. Range in mm (e.g. 1.5-10mm, 1.5mm to 10mm) OR single dimension in mm
        range_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*mm\b", text_norm)
        if range_match:
            v1 = float(range_match.group(1))
            v2 = float(range_match.group(2))
            specs["range_mm"] = f"{int(v1) if v1.is_integer() else v1}-{int(v2) if v2.is_integer() else v2}mm"
        else:
            dim_match = re.search(r"(\d+(?:\.\d+)?)\s*mm\b", text_norm)
            if dim_match:
                val = float(dim_match.group(1))
                specs["dimension_mm"] = f"{int(val) if val.is_integer() else val}mm"

        # 5. Stainless Material Grade (SS304, SS316, Grade 304, Grade 316)
        if "304" in text_norm or "ss304" in text_norm:
            specs["material_grade"] = "SS304"
        elif "316" in text_norm or "ss316" in text_norm:
            specs["material_grade"] = "SS316"

        # 6. Bearing Model & Suffix (6205-2RS, 6205-2Z, 6205)
        bearing_match = re.search(r"\b(6\d{3})(?:\s*-\s*([a-z0-9]+))?\b", text_norm)
        if bearing_match:
            specs["bearing_model"] = bearing_match.group(1)
            if bearing_match.group(2):
                specs["bearing_suffix"] = bearing_match.group(2).upper()

        # 7. Memory Capacity (8GB, 16GB, 32GB, 512GB)
        mem_match = re.search(r"\b(\d+)\s*(gb|tb|mb)\b", text_norm)
        if mem_match:
            specs["memory_capacity"] = f"{mem_match.group(1)}{mem_match.group(2).upper()}"

        return specs

    def _normalize_code(self, code: Optional[str]) -> Optional[str]:
        if not code:
            return None
        return re.sub(r"[^A-Z0-9]", "", str(code).upper().strip())
