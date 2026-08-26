"""
Post-Award Artifact Orchestration Service.
Connects canonical AwardRecord with configured artifact renderers via AwardRecordViewModel.
"""

from typing import Any, Dict, List, Optional, Tuple

from artifacts.models import ArtifactMetadata, ArtifactType
from artifacts.registry import get_renderer
from artifacts.view_models import build_award_view_model
from award.models import AwardArtifactPolicy, AwardRecord


def get_artifact_policy(settings_dict: Optional[Dict[str, Any]] = None) -> AwardArtifactPolicy:
    """Resolves active artifact generation policy from enterprise/tenant settings."""
    if not settings_dict:
        from app import services
        settings_dict = services.get_application_settings()
    
    return AwardArtifactPolicy(
        award_report=settings_dict.get("enable_award_report", True),
        award_workbook=settings_dict.get("enable_award_workbook", True),
        erp_export=settings_dict.get("enable_erp_export", True),
        supplier_requisition=settings_dict.get("enable_supplier_requisition", True)
    )


def render_award_artifact(
    award: AwardRecord,
    artifact_type: ArtifactType,
    rfq_title: str = ""
) -> Tuple[bytes, ArtifactMetadata]:
    """
    Renders an individual post-award artifact directly from canonical AwardRecord.
    """
    renderer_cls = get_renderer(artifact_type)
    if not renderer_cls:
        raise ValueError(f"No renderer registered for artifact type '{artifact_type}'.")
    
    # 1. Build domain-agnostic view model (performs all presentation formatting)
    vm = build_award_view_model(award, rfq_title=rfq_title)
    
    # 2. Invoke renderer (pure presentation projection)
    renderer = renderer_cls()
    return renderer.render(vm)


def get_configured_artifacts_manifest(
    award: AwardRecord,
    rfq_title: str = ""
) -> List[ArtifactMetadata]:
    """
    Returns metadata list of configured, active post-award artifacts for an AwardRecord.
    """
    policy = get_artifact_policy()
    manifest: List[ArtifactMetadata] = []
    
    vm = build_award_view_model(award, rfq_title=rfq_title)
    
    if policy.award_report:
        renderer_cls = get_renderer(ArtifactType.AWARD_REPORT)
        if renderer_cls:
            _, meta = renderer_cls().render(vm)
            manifest.append(meta)
            
    if policy.award_workbook:
        renderer_cls = get_renderer(ArtifactType.AWARD_WORKBOOK)
        if renderer_cls:
            _, meta = renderer_cls().render(vm)
            manifest.append(meta)
            
    if policy.erp_export:
        renderer_cls = get_renderer(ArtifactType.ERP_EXPORT)
        if renderer_cls:
            _, meta = renderer_cls().render(vm)
            manifest.append(meta)
            
    return manifest
