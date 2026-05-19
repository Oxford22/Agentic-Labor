"""Reference target callables used by the CI eval workflow.

These are deliberately minimal — the CI workflow runs them only to exercise
the eval pipeline end-to-end. Real Putsch services replace them with their
own target callables.
"""

from __future__ import annotations

from typing import Any


def identity_target(input_value: Any) -> Any:
    """Echo the input back as the output.

    Useful in CI as a no-op shim that lets us verify the harness wiring
    without depending on a live model endpoint.
    """
    if isinstance(input_value, dict) and "expected_output" in input_value:
        # Defensive: in test fixtures we sometimes pass the whole item.
        return input_value["expected_output"]
    return input_value


__all__ = ["identity_target"]
