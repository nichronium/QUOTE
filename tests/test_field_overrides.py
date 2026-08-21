from decimal import Decimal
from core.canonical_quote import (
    CanonicalQuote,
    QuoteItem,
    MatchStatus,
    ExtractionMetadata,
)


def test_field_level_override_preserves_only_human_fields():
    """
    TEST-EXT-03 (Field-level):
    1. Human corrects ONLY `matched_erp_item_code`.
    2. Re-extraction produces an improved `unit_price` and better `raw_description`.
    3. Merged result MUST take the new `unit_price` & `raw_description`,
       while PRESERVING the human's `matched_erp_item_code`.
    """
    # 1. Initial quote
    item = QuoteItem(
        line_index=0,
        raw_description="SKF Bearng 6205",  # typo in initial OCR
        quoted_qty=Decimal("10"),
        quoted_uom="PCS",
        unit_price=Decimal("500.00"),
    )
    # Human fixes the SKU match
    item.apply_human_override(
        field_name="matched_erp_item_code",
        new_value="ITEM-BEAR-6205-SKF",
        user="procurement_mgr"
    )
    item.apply_human_override(
        field_name="match_status",
        new_value=MatchStatus.MANUAL_MATCHED,
        user="procurement_mgr"
    )

    quote = CanonicalQuote(
        quote_id="Q-001",
        supplier_raw_name="ABC Traders",
        items=[item],
        extraction_metadata=ExtractionMetadata(
            source_file_name="quote.pdf",
            source_file_hash="hash1",
            parser_used="docling_v1"
        )
    )

    # 2. Improved Re-extraction
    reextracted_item = QuoteItem(
        line_index=0,
        raw_description="SKF Bearing 6205 (Cleaned OCR)",  # Improved text
        quoted_qty=Decimal("10"),
        quoted_uom="PCS",
        unit_price=Decimal("475.50"),                      # Improved price
        matched_erp_item_code=None,                         # Extractor doesn't know SKU
        match_status=MatchStatus.UNMATCHED
    )
    new_quote = CanonicalQuote(
        quote_id="Q-001",
        supplier_raw_name="ABC Traders",
        items=[reextracted_item],
        extraction_metadata=ExtractionMetadata(
            source_file_name="quote.pdf",
            source_file_hash="hash1_rerun",
            parser_used="docling_v2"
        )
    )

    # 3. Merge
    merged = quote.merge_reextraction(new_quote)

    # Assertions
    merged_line = merged.items[0]
    # Human-corrected fields preserved:
    assert merged_line.matched_erp_item_code == "ITEM-BEAR-6205-SKF"
    assert merged_line.match_status == MatchStatus.MANUAL_MATCHED
    assert "matched_erp_item_code" in merged_line.field_corrections

    # AI improvements accepted:
    assert merged_line.raw_description == "SKF Bearing 6205 (Cleaned OCR)"
    assert merged_line.unit_price == Decimal("475.50")
    assert merged_line.calculate_line_landed_cost() == Decimal("4755.00")
