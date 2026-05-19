"""Bootstrap a golden artifact JSON for a signature.

Workflow:

1. Run a real compile against the signature on staging.
2. Persist the resulting artifact metadata to ``tests/golden/<signature>.v<N>.json``.

The regression test in ``tests/test_regression.py`` then locks the signature's identity to that
golden — subsequent edits to the signature class must either match (no-op) or bump the version
and recompile.

Usage:

```bash
python scripts/seed_golden.py <signature_name>
```

Reads ``PUTSCH_COMPILE_*`` from env (same as the CLI).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from putsch_compile.artifacts import run_async
from putsch_compile.logging import configure_logging, get_logger
from putsch_compile.optimize import OptimizerHarness
from putsch_compile.registry import Registry
from putsch_compile.signatures import SIGNATURE_REGISTRY

_log = get_logger(__name__)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("signature_name", help="signature to compile + record as golden")
    parser.add_argument(
        "--actor",
        default="seed-golden@putsch.example",
        help="who triggered this — recorded in the artifact and golden",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "tests" / "golden",
    )
    args = parser.parse_args(argv)

    configure_logging()

    if args.signature_name not in SIGNATURE_REGISTRY:
        _log.error("seed.unknown_signature", name=args.signature_name)
        return 2

    sig = SIGNATURE_REGISTRY[args.signature_name]
    harness = OptimizerHarness()
    result = run_async(
        harness.compile_signature(args.signature_name, actor=args.actor, environment="staging")
    )

    registry = Registry()
    payload = run_async(registry.load_payload(result.artifact_id))

    golden = {
        "signature_name": args.signature_name,
        "signature_version": sig.meta().version,
        "signature_version_hash": sig.version_hash(),
        "model": result.selected_model,
        "compiled_instruction": payload.compiled_instruction[:2000],  # truncate to keep golden tidy
        "dataset_hash": result.dataset_hash,
        "seed": payload.seed,
        "holdout_accuracy": result.holdout_accuracy,
        "cost_eur_per_call": result.cost_eur_per_call,
        "compiled_at": result.compiled_at.isoformat(),
    }
    n = _next_version_number(args.out_dir, args.signature_name)
    out_path = args.out_dir / f"{args.signature_name}.v{n}.json"
    out_path.write_text(json.dumps(golden, indent=2, sort_keys=True), encoding="utf-8")
    _log.info("seed.golden_written", path=str(out_path), artifact_id=result.artifact_id)
    return 0


def _next_version_number(dir_: Path, signature_name: str) -> int:
    if not dir_.exists():
        return 1
    existing = list(dir_.glob(f"{signature_name}.v*.json"))
    n = 1
    for p in existing:
        try:
            n = max(n, int(p.stem.split(".v")[-1]) + 1)
        except ValueError:
            continue
    return n


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
