"""Tool registry (CapabilitySpec / ToolSpec)."""

from .exposure import LoadExactResult, ToolExposureState
from .registry import CapabilitySpec, ToolContext, ToolRegistry, ToolSpec, build_default_registry

__all__ = [
    "CapabilitySpec",
    "LoadExactResult",
    "ToolContext",
    "ToolExposureState",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
]
