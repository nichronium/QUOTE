"""
Centralized Semantic Registry for Document Understanding.
Provides unified, extensible alias dictionaries, regex patterns, and normalization mappings
for fields, table types, row types, charges, UOMs, currencies, and commercial terms.
"""

from enum import Enum
import re
from typing import Dict, List, Optional, Set


class TableType(str, Enum):
    MAIN_LINE_ITEMS = "MAIN_LINE_ITEMS"
    VOLUME_PRICING = "VOLUME_PRICING"
    DISCOUNT_TABLE = "DISCOUNT_TABLE"
    TAX_TABLE = "TAX_TABLE"
    FREIGHT_TABLE = "FREIGHT_TABLE"
    PACKING_TABLE = "PACKING_TABLE"
    CHARGES = "CHARGES"             # Quote-level surcharges (freight, packing, insurance, etc.)
    METADATA = "METADATA"           # Key-value document metadata block
    SUMMARY_TOTALS = "SUMMARY_TOTALS"
    TERMS = "TERMS"
    CONTACT_DETAILS = "CONTACT_DETAILS"
    NOTES = "NOTES"
    HISTORICAL_REFERENCE = "HISTORICAL_REFERENCE"
    UNKNOWN = "UNKNOWN"


# Column & Field Header Aliases with Semantic Categorization
COLUMN_ALIASES: Dict[str, List[str]] = {
    # 1. Specific Compound Identifiers
    "supplier_part_number": [
        r"supplier\s*part\s*number", r"supplier\s*part\s*no\.?", r"supplier\s*part\s*#",
        r"supplier\s*part\s*code", r"supplier\s*part\s*num", r"supplier\s*sku",
        r"supplier\s*item\s*code", r"supplier\s*item\s*no\.?", r"supplier\s*item\s*#",
        r"vendor\s*part\s*number", r"vendor\s*part\s*no\.?", r"vendor\s*part\s*#",
        r"vendor\s*part\s*code", r"vendor\s*part\s*num", r"vendor\s*sku",
        r"vendor\s*item\s*code", r"vendor\s*item\s*no\.?", r"vendor\s*item\s*#",
        r"seller\s*part\s*number", r"seller\s*part\s*no\.?", r"seller\s*sku"
    ],
    "manufacturer_part_number": [
        r"manufacturer\s*part\s*number", r"manufacturer\s*part\s*no\.?", r"manufacturer\s*part\s*#",
        r"manufacturer\s*part\s*code", r"manufacturer\s*pn", r"manufacturer\s*sku",
        r"mfg\s*part\s*number", r"mfg\s*part\s*no\.?", r"mfg\s*part\s*#", r"mfg\s*part\s*code",
        r"mfg\s*pn", r"mfg\s*part", r"mfg\s*code", r"mfg\s*item\s*code",
        r"oem\s*part\s*number", r"oem\s*part\s*no\.?", r"oem\s*part\s*#", r"oem\s*pn", r"oem\s*sku",
        r"item\s*code\s*/\s*mfg\s*pn", r"mfg\s*/\s*part\s*no\.?", r"mfg\s*part\s*/\s*pn"
    ],
    "internal_sku": [
        r"internal\s*sku", r"internal\s*item\s*code", r"internal\s*part\s*number",
        r"internal\s*part\s*no\.?", r"internal\s*part\s*#", r"internal\s*code",
        r"our\s*part\s*no\.?", r"our\s*part\s*number", r"our\s*sku",
        r"buyer\s*item\s*code", r"buyer\s*part\s*no\.?", r"client\s*part\s*no\.?"
    ],
    "part_number": [
        r"part\s*number", r"part\s*no\.?", r"part\s*#", r"part\s*code", r"part\s*num",
        r"item\s*code\s*/\s*sku", r"item\s*code", r"item\s*no\.?", r"item\s*#", r"item\s*num",
        r"sku", r"material\s*code", r"product\s*code", r"cat\s*no\.?", r"catalogue\s*no\.?",
        r"catalog\s*no\.?", r"model\s*no\.?", r"model\s*num", r"model", r"code"
    ],

    # 2. Document & Supplier Metadata
    "supplier": [
        r"supplier\s*name", r"supplier", r"vendor\s*name", r"vendor", r"seller\s*name",
        r"seller", r"company\s*name", r"company", r"party\s*name", r"party", r"quoted\s*by", r"m/s", r"from"
    ],
    "quote_number": [
        r"quote\s*no\.?", r"quotation\s*no\.?", r"quote\s*#", r"quote\s*num", r"ref\s*no\.?",
        r"reference\s*no\.?", r"quotation\s*ref", r"quote\s*ref", r"estimate\s*no\.?",
        r"inquiry\s*no\.?", r"rfq\s*no\.?", r"rfq\s*ref", r"offer\s*no\.?"
    ],
    "quote_date": [
        r"quote\s*date", r"quotation\s*date", r"dated", r"date", r"doc\s*date", r"po\s*date", r"rfq\s*date", r"valid\s*from", r"offer\s*date"
    ],
    "valid_until": [
        r"valid\s*(?:till|until)", r"validity", r"valid\s*up\s*to", r"expiry\s*date"
    ],
    "currency": [
        r"currency", r"curr", r"cur"
    ],

    # 3. Item Details
    "description": [
        r"description\s*/\s*particulars", r"description", r"item\s*description", r"product\s*description",
        r"item\s*name", r"product\s*name", r"particulars", r"material\s*description", r"material",
        r"item", r"product", r"specification", r"details", r"designation"
    ],
    "hsn": [
        r"hsn", r"sac", r"hsn\s*code", r"sac\s*code", r"tariff", r"hsn/sac"
    ],
    "qty": [
        r"order\s*qty", r"quoted\s*qty", r"required\s*qty", r"qty", r"quantity", r"qnty", r"pcs", r"nos"
    ],
    "uom": [
        r"unit\s*of\s*measure", r"uom\s*code", r"units", r"unit", r"uom", r"measure", r"um"
    ],
    "unit_price": [
        r"basic\s*rate", r"unit\s*rate", r"unit\s*price", r"rate/unit", r"price/unit",
        r"quoted\s*rate", r"selling\s*price", r"basic\s*price", r"base\s*rate", r"base\s*price",
        r"rate", r"price"
    ],

    # 4. Financial Adjustments & Taxes
    "discount": [
        r"disc\s*%", r"discount\s*%", r"discount\s*pct", r"discount", r"disc", r"rebate"
    ],
    "tax": [
        r"total\s*gst\s*%", r"total\s*tax\s*%", r"total\s*gst", r"total\s*tax",
        r"gst\s*rate\s*%", r"tax\s*rate\s*%", r"gst\s*rate", r"tax\s*rate",
        r"gst\s*%", r"tax\s*%", r"gst", r"tax", r"vat"
    ],
    "cgst": [r"cgst\s*%", r"cgst\s*rate", r"cgst"],
    "sgst": [r"sgst\s*%", r"sgst\s*rate", r"sgst", r"utgst\s*%", r"utgst\s*rate", r"utgst"],
    "igst": [r"igst\s*%", r"igst\s*rate", r"igst"],
    "cess": [r"cess\s*%", r"cess\s*rate", r"cess"],

    # 5. Line Totals & Logistics
    "amount": [
        r"net\s*amount", r"line\s*total", r"total\s*amount", r"extended\s*price", r"amount", r"value", r"total"
    ],
    "lead_time": [
        r"lead\s*time", r"delivery", r"delivery\s*time", r"dispatch\s*time", r"delivery\s*period"
    ],
    "moq": [
        r"moq", r"min\s*qty", r"minimum\s*order\s*qty", r"minimum\s*quantity"
    ],
    "min_qty": [
        r"min\s*qty", r"from\s*qty", r"qty\s*from", r"minimum\s*qty", r"lower\s*limit"
    ],
    "max_qty": [
        r"max\s*qty", r"to\s*qty", r"qty\s*to", r"maximum\s*qty", r"upper\s*limit"
    ]
}


# Standard UOM Canonical Set
STANDARD_UOM_SET: Set[str] = {
    "PCS", "NOS", "EA", "SET", "PAIR", "BOX", "PKT", "ROLL", "BAG",
    "MTR", "FT", "INCH", "MM", "CM", "KM", "YD",
    "KG", "G", "MG", "TON", "MT", "LBS",
    "LTR", "ML", "GAL", "KL",
    "SQM", "SQFT", "SQIN",
    "CUM", "CUFT"
}

UOM_MAP: Dict[str, str] = {
    "pcs": "PCS", "pc": "PCS", "pieces": "PCS", "piece": "PCS",
    "nos": "PCS", "no": "PCS", "number": "PCS", "numbers": "PCS",
    "ea": "PCS", "each": "PCS",
    "set": "SET", "sets": "SET",
    "pair": "PAIR", "pairs": "PAIR", "pr": "PAIR",
    "box": "BOX", "boxes": "BOX", "pkt": "PKT", "packet": "PKT",
    "packets": "PKT", "roll": "ROLL", "rolls": "ROLL", "bag": "BAG", "bags": "BAG",
    "mtr": "MTR", "mtrs": "MTR", "meter": "MTR", "meters": "MTR", "m": "MTR",
    "ft": "FT", "feet": "FT", "foot": "FT",
    "inch": "INCH", "inches": "INCH", "in": "INCH",
    "mm": "MM", "cm": "CM", "km": "KM", "yd": "YD", "yards": "YD",
    "kg": "KG", "kgs": "KG", "kilogram": "KG", "kilograms": "KG",
    "g": "G", "gm": "G", "gms": "G", "gram": "G", "grams": "G", "mg": "MG",
    "ton": "TON", "tons": "TON", "mt": "MT", "metric ton": "MT", "lbs": "LBS", "pound": "LBS", "pounds": "LBS",
    "ltr": "LTR", "ltrs": "LTR", "liter": "LTR", "liters": "LTR", "l": "LTR",
    "ml": "ML", "gal": "GAL", "gallon": "GAL", "gallons": "GAL", "kl": "KL",
    "sqm": "SQM", "sqft": "SQFT", "sqin": "SQIN", "cum": "CUM", "cuft": "CUFT"
}



# Currency Symbols & Normalization Mapping
CURRENCY_MAP: Dict[str, str] = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupees": "INR",
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "a$": "AUD", "aud": "AUD",
    "c$": "CAD", "cad": "CAD",
    "¥": "JPY", "jpy": "JPY", "cny": "CNY", "rmb": "CNY",
    "aed": "AED", "dirham": "AED", "sar": "SAR", "riyal": "SAR",
    "sgd": "SGD", "s$": "SGD"
}


# Document-level Surcharge Patterns
CHARGE_PATTERNS: Dict[str, List[str]] = {
    "FREIGHT": [
        r"freight\s*charges?", r"freight", r"transportation\s*charges?", r"transport(?:ation)?",
        r"shipping\s*charges?", r"shipping", r"courier\s*charges?", r"courier",
        r"carriage\s*charges?", r"cartage", r"delivery\s*charges?", r"logistics"
    ],
    "PACKING": [
        r"packing\s*charges?", r"packing", r"packing\s*&\s*forwarding", r"p&f\s*charges?", r"p&f",
        r"packaging\s*charges?", r"packaging", r"forwarding\s*charges?", r"forwarding", r"handling\s*charges?"
    ],

    "INSURANCE": [
        r"transit\s*insurance", r"insurance\s*charges?", r"insurance"
    ],
    "TOOLING": [
        r"tooling\s*charges?", r"die\s*charges?", r"mould\s*charges?", r"setup\s*charges?"
    ],
    "CUSTOMS": [
        r"customs\s*duty", r"clearing\s*charges?", r"import\s*duty", r"octroi"
    ],
    "OTHER": [
        r"miscellaneous\s*charges?", r"other\s*charges?", r"extra\s*charges?", r"surcharge"
    ]
}
