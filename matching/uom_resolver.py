"""
Dimension-Safe UOM Resolver.
Provides approved intra-dimension UOM conversions (e.g. MTR <hat> FT, KG <hat> G),
and enforces strict prevention of unapproved cross-dimension conversions (e.g. KG <hat> NOS)
without explicit item-master packaging factors.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Set

from matching.models import ItemMasterRecord, RFQLineItem, UOMConversionResult


class UOMResolver:
    """Dimension-safe UOM compatibility and conversion factor calculator."""

    DIMENSIONS_MAP: Dict[str, Set[str]] = {
        "LENGTH": {"MTR", "M", "FTLT", "FT", "FEET", "MM", "CM", "INCH", "IN", "YMDS", "YARD"},
        "WEIGHT": {"KG", "KGS", "G", "GM", "GRAMS", "TON", "MT", "LBS", "LB", "POUND"},
        "VOLUME": {"LTR", "LITER", "ML", "CUM", "M3", "GN", "GALLON"},
        "COUNT": {"PCS", "PC", "NOS", "NO", "UNIT", "UNITS", "EA", "EACH", "ITEM", "ITEMS", "PEA1"},
        "PACKAGING": {"BOX", "PKT", "PACK", "PKG", "ROLL", "SET", "SETS", "PAIR", "DOZ", "DOZEN"},
        "TIME_SERVICE": {"HOUR", "HR", "HRS", "DAY", "DAYS", "MONTH", "MTH", "YEAR", "YR", "SERVICE", "JOB", "LOT", "PROJECT"},
        "DIGITAL_LICENSE": {"LICENSE", "LIC", "USER", "USERS", "SEAT", "SEATS", "NODE", "NODES", "DEVICE", "DEVICES", "SUBSCRIPTION", "SUB"}
    }

    # Relative factors to base unit of each dimension (LENGTH = MTR, WEIGHT = KG, VOLUME = LTR, COUNT = PCS)
    STANDARD_RATES: Dict[str, Decimal] = {
        # Length (base: MTR)
        "MTR": Decimal("1.0"), "M": Decimal("1.0"),
        "FT": Decimal("0.3048"), "FTLT": Decimal("0.3048"), "FEET": Decimal("0.3048"),
        "MM": Decimal("0.001"), "CM": Decimal("0.01"),
        "INCH": Decimal("0.0254"), "IN": Decimal("0.0254"),
        "YARD": Decimal("0.9144"), "YMDS": Decimal("0.9144"),

        # Weight (base: KG)
        "KG": Decimal("1.0"), "KGS": Decimal("1.0"),
        "G": Decimal("0.001"), "GM": Decimal("0.001"), "GRAMS": Decimal("0.001"),
        "TON": Decimal("1000.0"), "MT": Decimal("1000.0"),
        "LBS": Decimal("0.453592"), "LB": Decimal("0.453592"), "POUND": Decimal("0.453592"),

        # Volume (base: LTR)
        "LTR": Decimal("1.0"), "LITER": Decimal("1.0"),
        "ML": Decimal("0.001"), "CUM": Decimal("1000.0"), "M3": Decimal("1000.0"),

        # Count (base: PCS)
        "PCS": Decimal("1.0"), "PC": Decimal("1.0"),
        "NOS": Decimal("1.0"), "NO": Decimal("1.0"),
        "UNIT": Decimal("1.0"), "UNITS": Decimal("1.0"),
        "EA": Decimal("1.0"), "EACH": Decimal("1.0"),
        "ITEM": Decimal("1.0"), "ITEMS": Decimal("1.0"),
        "PAIR": Decimal("2.0"), "DOZ": Decimal("12.0"), "DOZEN": Decimal("12.0"),

        # Time/Service base equivalence (1:1 unless approved factors exist)
        "HOUR": Decimal("1.0"), "HR": Decimal("1.0"), "HRS": Decimal("1.0"),
        "DAY": Decimal("1.0"), "DAYS": Decimal("1.0"),
        "MONTH": Decimal("1.0"), "MTH": Decimal("1.0"),
        "YEAR": Decimal("1.0"), "YR": Decimal("1.0"),
        "SERVICE": Decimal("1.0"), "JOB": Decimal("1.0"), "LOT": Decimal("1.0"), "PROJECT": Decimal("1.0"),

        # Digital / License base equivalence
        "LICENSE": Decimal("1.0"), "LIC": Decimal("1.0"),
        "USER": Decimal("1.0"), "USERS": Decimal("1.0"),
        "SEAT": Decimal("1.0"), "SEATS": Decimal("1.0"),
        "NODE": Decimal("1.0"), "NODES": Decimal("1.0"),
        "DEVICE": Decimal("1.0"), "DEVICES": Decimal("1.0"),
        "SUBSCRIPTION": Decimal("1.0"), "SUB": Decimal("1.0")
    }

    def resolve_uom_conversion(
        self,
        source_uom: Optional[str],
        target_uom: Optional[str],
        quoted_quantity: Optional[Decimal] = None,
        item_master: Optional[ItemMasterRecord] = None,
        rfq_line: Optional[RFQLineItem] = None
    ) -> UOMConversionResult:
        """
        Resolves UOM compatibility and computes conversion factor.
        Returns UOMConversionResult with converted_quantity if compatible.
        """
        src = (source_uom or "").upper().strip()
        tgt = (target_uom or "").upper().strip()

        # Handle missing UOMs gracefully
        if not src or not tgt:
            return UOMConversionResult(
                is_compatible=True,
                conversion_factor=Decimal("1.0"),
                converted_quantity=quoted_quantity,
                source_uom=source_uom or "N/A",
                target_uom=target_uom or "N/A",
                conversion_method="UNSPECIFIED_UOM"
            )

        # 1. Identical UOM
        if src == tgt:
            return UOMConversionResult(
                is_compatible=True,
                conversion_factor=Decimal("1.0"),
                converted_quantity=quoted_quantity,
                source_uom=source_uom,
                target_uom=target_uom,
                conversion_method="EXACT_UOM_MATCH"
            )

        # 2. Check Item-specific Approved Conversion Factors
        approved_factors = {}
        if item_master and hasattr(item_master, "approved_conversion_factors"):
            approved_factors.update(item_master.approved_conversion_factors)
        if rfq_line and hasattr(rfq_line, "approved_uom_conversions"):
            approved_factors.update(rfq_line.approved_uom_conversions)

        if src in approved_factors:
            factor = approved_factors[src]
            conv_qty = (quoted_quantity * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if quoted_quantity is not None else None
            return UOMConversionResult(
                is_compatible=True,
                conversion_factor=factor,
                converted_quantity=conv_qty,
                source_uom=source_uom,
                target_uom=target_uom,
                conversion_method="ITEM_MASTER_FACTOR"
            )

        # 3. Intra-Dimension Standard Conversion
        src_dim = self._get_dimension(src)
        tgt_dim = self._get_dimension(tgt)

        if src_dim and tgt_dim and src_dim == tgt_dim:
            src_rate = self.STANDARD_RATES.get(src)
            tgt_rate = self.STANDARD_RATES.get(tgt)
            if src_rate and tgt_rate and tgt_rate > Decimal("0.0"):
                factor = (src_rate / tgt_rate).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
                conv_qty = (quoted_quantity * factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if quoted_quantity is not None else None
                return UOMConversionResult(
                    is_compatible=True,
                    conversion_factor=factor,
                    converted_quantity=conv_qty,
                    source_uom=source_uom,
                    target_uom=target_uom,
                    conversion_method="STANDARD_DIMENSION"
                )

        # 4. Cross-Dimension or Unknown -> UOM INCOMPATIBLE
        return UOMConversionResult(
            is_compatible=False,
            conversion_factor=Decimal("0.0"),
            converted_quantity=None,
            source_uom=source_uom,
            target_uom=target_uom,
            conversion_method="INCOMPATIBLE",
            error_reason=f"Cross-dimension conversion from '{source_uom}' to '{target_uom}' requires approved item-specific packaging factor"
        )

    def _get_dimension(self, uom: str) -> Optional[str]:
        for dim_name, uom_set in self.DIMENSIONS_MAP.items():
            if uom in uom_set:
                return dim_name
        return None
