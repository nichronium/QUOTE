"""
Artifact Models, Enums & Metadata Contracts.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class ArtifactType(str, Enum):
    AWARD_REPORT = "AWARD_REPORT"
    AWARD_WORKBOOK = "AWARD_WORKBOOK"
    ERP_EXPORT = "ERP_EXPORT"
    SUPPLIER_REQUISITION = "SUPPLIER_REQUISITION"


class ArtifactFormat(str, Enum):
    PDF = "PDF"
    EXCEL = "EXCEL"
    CSV = "CSV"
    JSON = "JSON"


class ArtifactMetadata(BaseModel):
    artifact_id: str
    award_id: str
    rfq_id: str
    artifact_type: ArtifactType
    format: ArtifactFormat
    name: str
    filename: str
    mime_type: str
    size_bytes: int = 0
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    generated_by: str = "System"
    status: str = "GENERATED"
    download_url: Optional[str] = None
    supplier_id: Optional[str] = None
