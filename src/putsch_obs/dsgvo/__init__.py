"""DSGVO Art. 30 Verzeichnis von Verarbeitungstätigkeiten generator.

Public surface:

    from putsch_obs.dsgvo import register_service, generate_verzeichnis
"""

from __future__ import annotations

from putsch_obs.dsgvo.generator import generate_verzeichnis, generate_yaml
from putsch_obs.dsgvo.registry import (
    DataCategory,
    LegalBasis,
    ProcessingActivity,
    register_service,
    registered_activities,
)

__all__ = [
    "DataCategory",
    "LegalBasis",
    "ProcessingActivity",
    "generate_verzeichnis",
    "generate_yaml",
    "register_service",
    "registered_activities",
]
