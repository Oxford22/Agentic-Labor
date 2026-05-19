"""The harness: glues crews together into runnable workflows.

Public surface:
  - Pipeline    : sequential composition of crews, each wrapped in a
                  NodeAdapter so the trust boundary is enforced between them.
"""

from .pipeline import Pipeline

__all__ = ["Pipeline"]
