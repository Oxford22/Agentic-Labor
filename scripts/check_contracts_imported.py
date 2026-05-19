"""Each package's tests must import at least one putsch_contracts symbol.

Rule (c) of the integration gate. This script scans every
``packages/<name>/tests/`` tree (excluding ``putsch_contracts`` itself,
which is its own contract). Failing this is a merge-blocker.

Run: ``python scripts/check_contracts_imported.py``
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent

IMPORT_PATTERN = re.compile(
    r"(?:from\s+putsch_contracts(?:\.[a-z_]+)?\s+import\s)|(?:^\s*import\s+putsch_contracts)",
    re.MULTILINE,
)

PACKAGES_TO_SKIP: frozenset[str] = frozenset({"putsch_contracts"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=DEFAULT_REPO_ROOT,
        help="Repository root to scan (defaults to the script's parent).",
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    packages = root / "packages"

    if not packages.exists():
        print("no packages/ directory yet; skipping")
        return 0
    failures: list[str] = []
    for pkg_dir in sorted(packages.iterdir()):
        if not pkg_dir.is_dir() or pkg_dir.name in PACKAGES_TO_SKIP:
            continue
        tests_dir = pkg_dir / "tests"
        if not tests_dir.exists():
            failures.append(f"{pkg_dir.name}: missing tests/ directory; rule (c) cannot be checked")
            continue
        found = False
        for py in tests_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            if IMPORT_PATTERN.search(text):
                found = True
                break
        if not found:
            failures.append(
                f"{pkg_dir.name}: no test imports from putsch_contracts "
                "(rule (c) of docs/INTEGRATION_ORDER.md)"
            )

    if failures:
        sys.stderr.write("Contracts-import gate failures:\n")
        for f in failures:
            sys.stderr.write(f"  - {f}\n")
        return 1
    print("contracts-import gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
