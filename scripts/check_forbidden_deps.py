"""Forbid Anthropic / OpenAI in non-dev dependencies.

Per ``ARCHITECTURE.md``: La Plateforme (Mistral, Paris) is the only
frontier fallback. Anthropic / OpenAI may appear only in dev /
red-teaming optional groups. CI exits non-zero on any violation.

Walks every ``pyproject.toml`` in the workspace and inspects:

* ``project.dependencies``
* ``project.optional-dependencies.*`` except a whitelist of dev-only
  groups (``dev``, ``test``, ``tests``, ``redteam``, ``redteaming``,
  ``benchmarks``).
* ``tool.poetry.dependencies`` (poetry compat)

Run: ``python scripts/check_forbidden_deps.py``
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_PACKAGES: tuple[str, ...] = (
    "anthropic",
    "anthropic-bedrock",
    "anthropic-vertex",
    "openai",
    "openai-agents",
    "tiktoken",
    "langchain-anthropic",
    "langchain-openai",
    "llama-index-llms-anthropic",
    "llama-index-llms-openai",
)

DEV_ONLY_GROUPS: frozenset[str] = frozenset(
    {"dev", "test", "tests", "redteam", "redteaming", "benchmarks"}
)


def _normalize(spec: str) -> str:
    # "openai>=1.0; python_version >= '3.10'" -> "openai"
    head = re.split(r"[<>=!\[\s;,]", spec.strip(), maxsplit=1)[0]
    return head.lower()


def _check_list(items: list[str], where: str, errors: list[str]) -> None:
    for item in items:
        name = _normalize(item)
        if name in FORBIDDEN_PACKAGES:
            errors.append(f"{where}: {item!r} is forbidden in production paths")


def check_pyproject(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"{path}: cannot parse TOML — {exc}")
        return errors

    project = data.get("project", {})
    deps = project.get("dependencies", [])
    if isinstance(deps, list):
        _check_list(deps, f"{path}::project.dependencies", errors)

    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for group, items in optional.items():
            if group in DEV_ONLY_GROUPS:
                continue
            if isinstance(items, list):
                _check_list(items, f"{path}::optional-dependencies.{group}", errors)

    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    if isinstance(poetry_deps, dict):
        for name in poetry_deps:
            normalized = name.lower()
            if normalized in FORBIDDEN_PACKAGES:
                errors.append(f"{path}::tool.poetry.dependencies: {name!r} forbidden")

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

    errors: list[str] = []
    for path in root.rglob("pyproject.toml"):
        if "/.venv/" in path.as_posix() or "/node_modules/" in path.as_posix():
            continue
        errors.extend(check_pyproject(path))

    if errors:
        sys.stderr.write("Forbidden-dependency violations:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.stderr.write(
            "\nPer ARCHITECTURE.md, Anthropic / OpenAI may appear only under\n"
            "optional-dependencies in groups: " + ", ".join(sorted(DEV_ONLY_GROUPS)) + "\n"
        )
        return 1
    print("forbidden-deps: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
