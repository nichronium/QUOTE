import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import services

# 1. Process synthetic Excel quote
excel_path = Path("datasets/synthetic/sample_vendor_quote.xlsx")
excel_test = services.create_test("Benchmark - Super Fasteners (Excel)")
test_id_1 = excel_test["test_id"]
with open(excel_path, "rb") as f:
    services.upload_source_file(test_id_1, excel_path.name, f.read())
res_1 = services.run_extraction(test_id_1)
print("Excel Test (" + test_id_1 + "): Status = " + res_1["metadata"]["status"] + ", Lines = " + str(res_1["metadata"]["line_count"]) + ", Landed Cost = " + str(res_1["report"]["total_landed_cost"]))

# 2. Process synthetic PDF quote
pdf_path = Path("datasets/synthetic/sample_vendor_quote.pdf")
pdf_test = services.create_test("Benchmark - Apex Components (PDF)")
test_id_2 = pdf_test["test_id"]
with open(pdf_path, "rb") as f:
    services.upload_source_file(test_id_2, pdf_path.name, f.read())
res_2 = services.run_extraction(test_id_2)
print("PDF Test (" + test_id_2 + "): Status = " + res_2["metadata"]["status"] + ", Lines = " + str(res_2["metadata"]["line_count"]) + ", Landed Cost = " + str(res_2["report"]["total_landed_cost"]))
