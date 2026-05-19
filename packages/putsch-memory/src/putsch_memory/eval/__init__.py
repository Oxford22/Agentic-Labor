"""Eval harness for putsch-memory.

Three eval suites:

1. `temporal_correctness` — adapted LongMemEval on a German-business
   scenario set. The headline metric for the EU AI Act story.
2. `entity_disambiguation` — multi-vendor / multi-customer scenarios
   that exercise the reconciliation layer.
3. `reconstruction_accuracy` — given a past trace, reconstruct the
   agent's belief state. The compliance test.

Each suite is independently runnable; `cli.py` exposes a `--suite`
selector. CI runs all three on every PR; production runs them weekly.
"""

from putsch_memory.eval.entity_disambiguation import (
    DisambiguationCase,
    run_entity_disambiguation,
)
from putsch_memory.eval.reconstruction_accuracy import (
    ReconstructionCase,
    run_reconstruction_accuracy,
)
from putsch_memory.eval.temporal_correctness import (
    TemporalCase,
    run_temporal_correctness,
)

__all__ = [
    "DisambiguationCase",
    "ReconstructionCase",
    "TemporalCase",
    "run_entity_disambiguation",
    "run_reconstruction_accuracy",
    "run_temporal_correctness",
]
