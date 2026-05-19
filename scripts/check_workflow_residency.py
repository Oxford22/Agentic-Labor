"""Forbid non-EU regions and ``:latest`` Docker tags in workflow files.

Walks ``.github/workflows/*.yml`` plus any ``docker-compose*.yml``,
``deploy/**/*.yml``, ``deploy/**/*.tf`` and fails CI if any of these
patterns appear:

* ``us-east-`` or ``us-west-`` or ``us-gov-`` or ``ap-`` etc.
* A Docker image reference without a tag or with ``:latest``.

This is a coarse textual check on purpose: the alternative is parsing
every config format we use (YAML, HCL, Helm). The textual check is
correct for the patterns we care about and gives a clear error.

Run: ``python scripts/check_workflow_residency.py``
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_REGION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"\b{prefix}[a-z0-9\-]+", re.IGNORECASE)
    for prefix in (
        "us-east-",
        "us-west-",
        "us-gov-",
        "ap-south-",
        "ap-southeast-",
        "ap-northeast-",
        "sa-east-",
        "af-south-",
        "ca-central-",
        "cn-north-",
        "cn-northwest-",
        "me-south-",
        "me-central-",
    )
)

# image: foo, image: foo:latest, image: registry/foo:latest
LATEST_TAG = re.compile(
    r"^\s*image:\s*[\"']?([a-zA-Z0-9.\-_/]+)(:latest)?[\"']?\s*$",
    re.MULTILINE,
)
PINNED_TAG = re.compile(
    r"^\s*image:\s*[\"']?[a-zA-Z0-9.\-_/]+:([A-Za-z0-9._\-]+)(@sha256:[0-9a-f]{64})?[\"']?\s*$",
    re.MULTILINE,
)

ALLOWLIST_GLOBS: tuple[str, ...] = (
    # Document examples are not workflows.
    "ARCHITECTURE.md",
    "README.md",
    "docs/",
    "packages/putsch_contracts/src/putsch_contracts/residency.py",
    "scripts/check_workflow_residency.py",
)


def _is_allowlisted(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return False
    return any(
        rel == g or rel.startswith(g.rstrip("/") + "/") if g.endswith("/") else rel == g
        for g in ALLOWLIST_GLOBS
    )


def _scan_regions(text: str, path: Path, errors: list[str]) -> None:
    for pat in FORBIDDEN_REGION_PATTERNS:
        for m in pat.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            errors.append(f"{path}:{line_no}: forbidden region reference {m.group(0)!r}")


def _scan_image_tags(text: str, path: Path, errors: list[str]) -> None:
    for m in LATEST_TAG.finditer(text):
        # Two cases collapse to "untagged or :latest" here.
        line_no = text[: m.start()].count("\n") + 1
        ref = m.group(0).strip()
        if ":" not in ref.split("image:", 1)[1]:
            errors.append(f"{path}:{line_no}: image without explicit tag: {ref}")
        elif ref.endswith(":latest") or ref.endswith(":latest'") or ref.endswith(':latest"'):
            errors.append(f"{path}:{line_no}: image pinned to :latest: {ref}")


def _scan_files(root: Path) -> list[str]:
    errors: list[str] = []
    candidate_globs = (
        ".github/workflows/*.yml",
        ".github/workflows/*.yaml",
        "deploy/**/*.yml",
        "deploy/**/*.yaml",
        "deploy/**/*.tf",
        "**/docker-compose*.yml",
        "**/docker-compose*.yaml",
        "packages/**/deploy/**/*.yml",
    )
    seen: set[Path] = set()
    for glob in candidate_globs:
        for path in root.glob(glob):
            if path in seen or _is_allowlisted(path, root):
                continue
            if "/.venv/" in path.as_posix() or "/node_modules/" in path.as_posix():
                continue
            seen.add(path)
            text = path.read_text(encoding="utf-8")
            _scan_regions(text, path, errors)
            _scan_image_tags(text, path, errors)
    return errors


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
    errors = _scan_files(root)
    if errors:
        sys.stderr.write("EU-residency / image-pin violations:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.stderr.write(
            "\nPer ARCHITECTURE.md: every region must be eu-* / hetzner-*,\n"
            "and every image must be pinned (preferably with a digest).\n"
        )
        return 1
    print("workflow-residency: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
