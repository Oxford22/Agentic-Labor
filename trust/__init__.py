"""Trust-boundary primitives applied across every untrusted-input surface.

Defends six surfaces in the AP/Stammdaten threat model:
  ocr, datev, github, search, langfuse, git, env, worker

Public surface:
  - Source              : the source taxonomy enum
  - wrap_external       : envelope untrusted content in <external_content>
  - INSTRUCTION_HIERARCHY, ORCHESTRATOR_HEADER, WORKER_HEADER
                        : system-prompt blocks that name the hierarchy
"""

from .hierarchy import (
    INSTRUCTION_HIERARCHY,
    ORCHESTRATOR_HEADER,
    WORKER_HEADER,
)
from .wrappers import Source, contains_external_envelope, wrap_external

__all__ = [
    "Source",
    "wrap_external",
    "contains_external_envelope",
    "INSTRUCTION_HIERARCHY",
    "ORCHESTRATOR_HEADER",
    "WORKER_HEADER",
]
