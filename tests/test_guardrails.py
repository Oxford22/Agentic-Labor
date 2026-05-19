"""Guardrail scripts must catch the violations they claim to catch.

These tests run the scripts on synthetic temporary trees so we know the
CI gate has teeth — not on the real repo state.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _run(script: str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_forbidden_deps_passes_on_clean_tree(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n'
        'dependencies = ["pydantic>=2", "structlog"]\n'
        "[project.optional-dependencies]\n"
        'dev = ["pytest", "anthropic"]\n',
        encoding="utf-8",
    )
    result = _run("check_forbidden_deps.py", tmp_path)
    assert result.returncode == 0, result.stderr


def test_forbidden_deps_catches_openai_in_runtime(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["openai>=1.0"]\n',
        encoding="utf-8",
    )
    result = _run("check_forbidden_deps.py", tmp_path)
    assert result.returncode == 1
    assert "openai" in result.stderr.lower()


def test_forbidden_deps_catches_anthropic_in_non_dev_optional(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n'
        'dependencies = ["pydantic>=2"]\n'
        "[project.optional-dependencies]\n"
        'production = ["anthropic>=0.34"]\n',
        encoding="utf-8",
    )
    result = _run("check_forbidden_deps.py", tmp_path)
    assert result.returncode == 1
    assert "anthropic" in result.stderr.lower()


def test_workflow_residency_passes_on_eu_only(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-24.04\n"
        "    env:\n"
        "      AWS_REGION: eu-central-1\n"
        "    steps:\n"
        "      - run: echo hetzner-fsn1\n",
        encoding="utf-8",
    )
    (workflows / "compose.yml").write_text(
        "services:\n  pg:\n    image: postgres:16.4@sha256:" + "0" * 64 + "\n",
        encoding="utf-8",
    )
    result = _run("check_workflow_residency.py", tmp_path)
    assert result.returncode == 0, result.stderr


def test_workflow_residency_catches_us_east_1(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "deploy.yml").write_text(
        "env:\n  AWS_REGION: us-east-1\n",
        encoding="utf-8",
    )
    result = _run("check_workflow_residency.py", tmp_path)
    assert result.returncode == 1
    assert "us-east-1" in result.stderr


def test_workflow_residency_catches_latest_tag(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: nginx:latest\n",
        encoding="utf-8",
    )
    result = _run("check_workflow_residency.py", tmp_path)
    assert result.returncode == 1
    assert "latest" in result.stderr.lower()


def test_workflow_residency_catches_untagged_image(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: nginx\n",
        encoding="utf-8",
    )
    result = _run("check_workflow_residency.py", tmp_path)
    assert result.returncode == 1


def test_contracts_imported_passes_when_test_imports(tmp_path: Path) -> None:
    pkg = tmp_path / "packages" / "putsch_sample" / "tests"
    pkg.mkdir(parents=True)
    (pkg / "test_x.py").write_text(
        "from putsch_contracts import Invoice\n\ndef test_x() -> None:\n    assert Invoice\n",
        encoding="utf-8",
    )
    contracts_tests = tmp_path / "packages" / "putsch_contracts" / "tests"
    contracts_tests.mkdir(parents=True)
    (contracts_tests / "test_self.py").write_text(
        "def test_self() -> None: pass\n", encoding="utf-8"
    )
    result = _run("check_contracts_imported.py", tmp_path)
    assert result.returncode == 0, result.stderr


def test_contracts_imported_fails_when_test_does_not_import(tmp_path: Path) -> None:
    pkg = tmp_path / "packages" / "putsch_sample" / "tests"
    pkg.mkdir(parents=True)
    (pkg / "test_x.py").write_text(
        "def test_x() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    result = _run("check_contracts_imported.py", tmp_path)
    assert result.returncode == 1
    assert "putsch_sample" in result.stderr


def test_real_repo_passes_all_guardrails() -> None:
    """The bootstrap commit must itself satisfy every guardrail."""
    for script in (
        "check_forbidden_deps.py",
        "check_workflow_residency.py",
    ):
        result = _run(script, REPO_ROOT)
        assert result.returncode == 0, f"{script} failed:\n{result.stderr}"
