"""
Pluggable Post-Award Artifact Renderer Registry.
"""

from typing import Callable, Dict, Optional, Tuple, Type
from artifacts.models import ArtifactMetadata, ArtifactType
from artifacts.view_models import AwardRecordViewModel


class BaseArtifactRenderer:
    """Base interface for all post-award artifact renderers."""
    artifact_type: ArtifactType
    
    def render(self, vm: AwardRecordViewModel, **kwargs) -> Tuple[bytes, ArtifactMetadata]:
        raise NotImplementedError


_RENDERER_REGISTRY: Dict[ArtifactType, Type[BaseArtifactRenderer]] = {}


def register_renderer(artifact_type: ArtifactType, renderer_cls: Type[BaseArtifactRenderer]):
    _RENDERER_REGISTRY[artifact_type] = renderer_cls


def get_renderer(artifact_type: ArtifactType) -> Optional[Type[BaseArtifactRenderer]]:
    return _RENDERER_REGISTRY.get(artifact_type)
