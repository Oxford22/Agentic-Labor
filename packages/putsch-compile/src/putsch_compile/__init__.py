"""putsch_compile — compiled prompts + model routing + continuous optimization.

The strategic asset of the Putsch agent stack. See README.md for the compilation philosophy.

Public surface is deliberately narrow. Internal modules are not stable API.
"""

from __future__ import annotations

from putsch_compile.config import Settings, get_settings
from putsch_compile.exceptions import (
    AdapterError,
    CompilationError,
    DatasetError,
    OptimizerError,
    RegistryError,
    RoutingError,
)
from putsch_compile.registry import CompiledArtifactRecord, Registry
from putsch_compile.routing import ModelTier, Router
from putsch_compile.signatures import SIGNATURE_REGISTRY, PutschSignature, SignatureMeta

__all__ = [
    "SIGNATURE_REGISTRY",
    "AdapterError",
    "CompilationError",
    "CompiledArtifactRecord",
    "DatasetError",
    "ModelTier",
    "OptimizerError",
    "PutschSignature",
    "Registry",
    "RegistryError",
    "Router",
    "RoutingError",
    "Settings",
    "SignatureMeta",
    "get_settings",
]

__version__ = "0.1.0"
