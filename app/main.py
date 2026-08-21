"""
Quote Intelligence Extraction Lab Web Application.
Main FastAPI entry point.
"""

from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes import router

app = FastAPI(
    title="Quote Intelligence — Extraction Lab",
    description="Development and extraction benchmark test harness for supplier quotation processing.",
    version="0.1.0"
)

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    print("Starting Quote Intelligence Extraction Lab at http://127.0.0.1:8000 ...")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
