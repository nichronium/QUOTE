# Quote Intelligence (QI)

> **Enterprise Procurement Intelligence & RFQ Evaluation Engine**  
> *The deterministic middleware between messy supplier quotations and ERP/P2P systems.*

---

## Overview

**Quote Intelligence** automates the end-to-end commercial evaluation of industrial supplier quotations. It transforms unstructured, multi-format vendor quotes (`.xlsx`, `.csv`, `.pdf`) into structured, normalized procurement matrices with deterministic landed-cost calculations, fuzzy/semantic Item Master alignment, split-sourcing optimizations, and immutable award decisions with procurement execution artifacts.

---

## Core Capabilities

1. **Robust Multi-Format Ingestion**
   - Heuristic spreadsheet extraction: merged cells, multi-sheet topological scoring, formula cell evaluation, and compound GST taxes.
   - Vector PDF extraction with bounding box reconstruction.
   - Strict provenance tracking linking every extracted field directly to raw cell coordinates.

2. **Deterministic Matching Engine**
   - 4-Tier Matching Cascade: Exact SKU $\rightarrow$ Manufacturer Part Number (MPN) $\rightarrow$ Approved Supplier Catalog Part Number $\rightarrow$ RapidFuzz Token Sort Ratio with specification guardrails.
   - UOM Conversion Engine supporting dimensionally compatible units (length, weight, volume, packaging factors).
   - Review Center: Human-in-the-loop exception queue for ambiguous items with persistent decision locking.

3. **Commercial Evaluation & Landed Cost Normalization**
   - Multi-currency live & cached foreign exchange (FX) conversion with manual override capabilities.
   - Comprehensive commercial charges: freight allocation (proportional, per-item, flat), customs duty, compound GST (CGST/SGST/IGST), and cash discounts.
   - Volume tier resolution and lowest-cost line item split-sourcing optimization.

4. **Tamper-Resistant Award Decision & Immutability**
   - Server-side Decimal validation enforcing non-negative allocations and RFQ quantity constraints.
   - Authoritative pricing reconstruction from comparison snapshots.
   - Revision audit trails on award reopening and re-finalization.

5. **Procurement Execution Artifacts**
   - **Multi-Tab Excel Award Workbook:** 4 sheets covering Executive Summary, Line Allocation, Supplier Matrix, and Audit Log.
   - **Supplier-Specific Purchase Order Requisitions:** Branded Excel & PDF requisitions itemizing evaluated awards.
   - **Management Executive PDF Report:** High-level sourcing summary and cost reduction analysis.
   - **ERP Flat CSV Export:** Ready for P2P/ERP direct ingestion.

---

## Architecture & Technology Stack

| Layer | Technologies |
| :--- | :--- |
| **Backend Framework** | [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [Starlette](https://www.starlette.io/) |
| **Data Validation & Typing** | [Pydantic v2](https://docs.pydantic.dev/), Python `decimal.Decimal` (exact financial precision) |
| **Document Parsers** | [`openpyxl`](https://openpyxl.readthedocs.io/), [`pypdf`](https://pypdf.readthedocs.io/), Python `csv` |
| **Matching & Fuzzy Search** | [`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz) |
| **Document Generation** | [`reportlab`](https://www.reportlab.com/) (Platypus Flowables), `openpyxl` |
| **Frontend UI** | Semantic HTML5, CSS3 Custom Properties (Multi-theme support: Velvet Slate, Champagne Silk, Aurora Teal), Vanilla JavaScript |
| **Quality & Testing** | [`pytest`](https://docs.pytest.org/), `pytest-cov`, `ruff`, `mypy` |

---

## Project Structure

```
quote_intelligence/
├── app/                      # Web application layer
│   ├── main.py               # FastAPI application entrypoint
│   ├── routes.py             # HTTP routes & API endpoints
│   ├── services.py           # Domain services & persistence orchestrator
│   └── templates/            # Jinja2 templates (RFQs, Quotes, Matching, Comparisons, Award)
├── award/                    # Award execution & document export engine
│   └── exporter.py           # Excel, PDF, and CSV artifact generators
├── comparison/               # Commercial comparison & landed cost engine
│   ├── calculator.py         # Multi-tax, freight, and FX landed price calculations
│   └── ranking.py            # Single-supplier and split-sourcing ranking models
├── core/                     # Canonical models & domain schemas
│   └── canonical_quote.py    # Immutable CanonicalQuote & QuoteItem schemas
├── datasets/                 # Test suites & benchmark datasets
│   ├── ground_truth/         # Golden benchmark records
│   ├── procurement_benchmark/# Industrial item master & RFQ generator
│   └── synthetic/            # Synthetic sample vendor quotes
├── extraction/               # Normalization & parsing pipelines
├── matching/                 # SKU & Item Master matching engine
├── parsers/                  # Excel, CSV, and PDF parser adapters
└── tests/                    # 187+ automated test suites
```

---

## Getting Started

### 1. Prerequisites
- Python 3.10 or higher
- Git

### 2. Installation

Clone the repository and set up a virtual environment:

```bash
git clone https://github.com/your-username/quote-intelligence.git
cd quote-intelligence

python -m venv .venv
# On Windows:
.\.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -e .
```

### 3. Run the Development Server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open your browser at `http://localhost:8000`.

---

## Running Automated Tests

Run the full pytest suite (187+ tests including adversarial parsing, landed cost verification, and award integrity tests):

```bash
pytest tests/ -v
```

---

## Procurement Workflow Lifecycle

```
[ Step 1: RFQ Scope ]
       │ Define requested line items, quantities, UOMs, and target prices
       ▼
[ Step 2: Ingest Quotations ]
       │ Upload supplier quotes (.xlsx, .csv, .pdf)
       ▼
[ Step 3: Review Center & Matching ]
       │ Auto-match against Item Master catalog; confirm ambiguous items
       ▼
[ Step 4: Commercial Evaluation ]
       │ Normalize landed costs, currencies, duties, and volume tiers
       ▼
[ Step 5: Award Decision & Split Sourcing ]
       │ Allocate quantities across suppliers, lock award immutability
       ▼
[ Execution Artifacts ]
       │ Download Excel Workbooks, Executive PDFs, and Supplier Requisitions
```

---

## License

MIT License.
