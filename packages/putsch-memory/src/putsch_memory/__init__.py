"""putsch_memory — temporal knowledge graph memory backbone.

Public surface kept deliberately small. Internals (the Graphiti driver, the
Cypher generator, the migration runner) are addressable via submodules but
should not be reached for from agent code.
"""

from putsch_memory.config import Settings, settings
from putsch_memory.exceptions import (
    ConflictDetected,
    MemoryDegraded,
    MissingProvenance,
    PutschMemoryError,
    TemporalIntegrityError,
)
from putsch_memory.graphiti_client import MemoryClient, ProvenanceContext
from putsch_memory.ontology import (
    BUSINESS_GRAPH,
    Bestellung,
    Buchung,
    Buchungsperiode,
    Confidence,
    Fact,
    Konto,
    Kunde,
    Lieferant,
    Mahnverfahren,
    Material,
    Mitarbeiter,
    Rechnung,
    SourceSystem,
    Standort,
    Tochtergesellschaft,
    ValidityWindow,
    Wareneingang,
    Zollvorgang,
)

__version__ = "0.1.0"

__all__ = [
    "BUSINESS_GRAPH",
    "Bestellung",
    "Buchung",
    "Buchungsperiode",
    "Confidence",
    "ConflictDetected",
    "Fact",
    "Konto",
    "Kunde",
    "Lieferant",
    "Mahnverfahren",
    "Material",
    "MemoryClient",
    "MemoryDegraded",
    "MissingProvenance",
    "Mitarbeiter",
    "ProvenanceContext",
    "PutschMemoryError",
    "Rechnung",
    "Settings",
    "SourceSystem",
    "Standort",
    "TemporalIntegrityError",
    "Tochtergesellschaft",
    "ValidityWindow",
    "Wareneingang",
    "Zollvorgang",
    "__version__",
    "settings",
]
