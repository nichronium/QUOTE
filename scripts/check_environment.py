"""
Environment Verification Script.
Checks python interpreter, virtualenv location, and imports of all required packages.
"""

import importlib
import sys
from pathlib import Path

REQUIRED_PACKAGES = [
    ("pydantic", "pydantic", True),
    ("pytest", "pytest", True),
    ("openpyxl", "openpyxl", True),
    ("pypdf", "pypdf", True),
    ("rapidfuzz", "rapidfuzz", True),
    ("python-dateutil", "dateutil", True),
    ("python-dotenv", "dotenv", True),
    ("typer", "typer", True),
    ("rich", "rich", True),
    ("httpx", "httpx", True),
    ("tenacity", "tenacity", True),
    ("pytest-cov", "pytest_cov", False),
    ("ruff", "ruff", False),
    ("mypy", "mypy", False),
    ("docling", "docling", False),
]

def check_env() -> int:
    executable = sys.executable
    version = sys.version.replace("\n", " ")
    
    print("=" * 70)
    print("QUOTE INTELLIGENCE - ENVIRONMENT VERIFICATION")
    print("=" * 70)
    print(f"Python Executable: {executable}")
    print(f"Python Version   : {version}")
    
    # Verify it is in quote_intelligence/.venv
    is_venv = ".venv" in executable and "quote_intelligence" in executable
    print(f"Using Project Venv: {'YES (PASS)' if is_venv else 'NO (FAIL)'}")
    print("-" * 70)
    print(f"{'PACKAGE':<20} {'IMPORT NAME':<15} {'STATUS':<10} {'VERSION / ERROR'}")
    print("-" * 70)

    mandatory_failed = 0

    for pkg_name, import_name, is_mandatory in REQUIRED_PACKAGES:
        try:
            mod = importlib.import_module(import_name)
            pkg_version = getattr(mod, "__version__", "installed")
            print(f"{pkg_name:<20} {import_name:<15} {'PASS':<10} {pkg_version}")
        except Exception as e:
            err_msg = str(e).split("\n")[0][:35]
            status = "FAILED" if is_mandatory else "OPTIONAL_FAIL"
            if is_mandatory:
                mandatory_failed += 1
            print(f"{pkg_name:<20} {import_name:<15} {status:<10} {err_msg}")

    print("=" * 70)
    if not is_venv:
        print("CRITICAL: Python executable is NOT pointing to quote_intelligence/.venv!")
        return 1

    if mandatory_failed > 0:
        print(f"CRITICAL: {mandatory_failed} mandatory dependencies are missing!")
        return 1

    print("All mandatory dependencies are verified successfully.")
    return 0

if __name__ == "__main__":
    sys.exit(check_env())
