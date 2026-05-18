"""``putsch-compile`` — the human entrypoint.

Subcommands:

* ``compile <signature>``             — run GEPA against the dataset, persist the artifact.
* ``promote --artifact <id>``         — flip the active artifact in the registry for an environment.
* ``rollback <signature>``            — flip back to the previous active artifact.
* ``history <signature>``             — list the last 25 artifacts for a signature.
* ``sync-feedback <signature>``       — pull Langfuse annotations into the dataset file.
* ``validate-datasets [<path> ...]``  — Pydantic-validate every dataset file (CI uses this).

Everything writes structured logs to stdout. Exit codes: 0 = ok, 1 = compilation error, 2 =
regression halted the pipeline, 3 = unknown signature / dataset, 4 = registry error.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from putsch_compile.artifacts import run_async
from putsch_compile.config import get_settings
from putsch_compile.exceptions import (
    CompilationError,
    DatasetError,
    OptimizerError,
    RegistryError,
    RegressionError,
)
from putsch_compile.feedback import FeedbackSync, validate_dataset_file
from putsch_compile.logging import configure_logging, correlation_scope, get_logger
from putsch_compile.optimize import OptimizerHarness
from putsch_compile.registry import Registry
from putsch_compile.signatures import SIGNATURE_REGISTRY

_log = get_logger(__name__)


def _exit(code: int, exc: CompilationError) -> None:
    _log.error("cli.error", code=exc.code, message=str(exc), context=exc.context)
    sys.exit(code)


@click.group(help="putsch-compile — compiled prompts + model routing for the Putsch agent stack.")
@click.option("--log-level", default="INFO", show_default=True)
@click.option("--log-json/--log-pretty", default=True, show_default=True)
def main(log_level: str, log_json: bool) -> None:
    configure_logging(level=log_level, json=log_json)


@main.command("list-signatures", help="List every registered signature.")
def list_signatures() -> None:
    for name in sorted(SIGNATURE_REGISTRY):
        sig = SIGNATURE_REGISTRY[name]
        meta = sig.meta()
        click.echo(
            f"{name}  v{meta.version}  threshold={meta.accuracy_threshold:.2f}  "
            f"owner={meta.owner_team.value}  hash={sig.version_hash()}"
        )


@main.command("compile", help="Compile a signature with GEPA against its dataset.")
@click.argument("signature_name")
@click.option("--dataset", type=click.Path(path_type=Path), default=None)
@click.option("--actor", default="cli@local", help="Who triggered this compilation.")
@click.option(
    "--env",
    type=click.Choice(["dev", "staging", "prod"]),
    default="staging",
    show_default=True,
)
def cmd_compile(signature_name: str, dataset: Path | None, actor: str, env: str) -> None:
    with correlation_scope(command="compile", signature=signature_name):
        try:
            harness = OptimizerHarness()
            result = run_async(
                harness.compile_signature(
                    signature_name,
                    dataset_path=dataset,
                    actor=actor,
                    environment=env,
                )
            )
        except RegressionError as exc:
            _exit(2, exc)
        except (DatasetError, OptimizerError) as exc:
            _exit(1, exc)
        except RegistryError as exc:
            _exit(4, exc)

        _print_result(result)


@main.command("promote", help="Promote a compiled artifact to active in an environment.")
@click.option("--artifact", "artifact_id", required=True)
@click.option(
    "--env",
    type=click.Choice(["dev", "staging", "prod"]),
    required=True,
)
@click.option("--actor", required=True, help="LDAP / email of the promoter.")
def cmd_promote(artifact_id: str, env: str, actor: str) -> None:
    with correlation_scope(command="promote", artifact=artifact_id, env=env):
        registry = Registry()
        try:
            entry = run_async(
                registry.promote(artifact_id, environment=env, promoted_by=actor)
            )
        except RegistryError as exc:
            _exit(4, exc)
        click.echo(
            f"promoted {entry.signature_name} to {entry.artifact_id} in {env} "
            f"(previous: {entry.previous_artifact_id or 'none'})"
        )


@main.command("rollback", help="Roll back a signature to the previous active artifact.")
@click.argument("signature_name")
@click.option(
    "--env",
    type=click.Choice(["dev", "staging", "prod"]),
    required=True,
)
@click.option("--actor", required=True)
def cmd_rollback(signature_name: str, env: str, actor: str) -> None:
    with correlation_scope(command="rollback", signature=signature_name, env=env):
        registry = Registry()
        try:
            entry = run_async(
                registry.rollback(signature_name, environment=env, promoted_by=actor)
            )
        except RegistryError as exc:
            _exit(4, exc)
        click.echo(f"rolled back {signature_name}/{env} to {entry.artifact_id}")


@main.command("history", help="List recent artifacts for a signature.")
@click.argument("signature_name")
@click.option("--limit", default=25, show_default=True)
def cmd_history(signature_name: str, limit: int) -> None:
    registry = Registry()
    records = run_async(registry.history(signature_name, limit=limit))
    for r in records:
        click.echo(
            f"{r.created_at.isoformat()}  {r.id}  model={r.model}  "
            f"holdout={r.holdout_accuracy:.4f}  €/call={r.cost_eur_per_call:.6f}  actor={r.actor}"
        )


@main.command("sync-feedback", help="Pull Langfuse annotations into the dataset for a signature.")
@click.argument("signature_name")
@click.option("--dry-run", is_flag=True)
def cmd_sync_feedback(signature_name: str, dry_run: bool) -> None:
    with correlation_scope(command="sync-feedback", signature=signature_name):
        try:
            sync = FeedbackSync(dry_run=dry_run)
            report = run_async(sync.sync_signature(signature_name))
        except DatasetError as exc:
            _exit(3, exc)
        click.echo(
            f"pulled={report.pulled} appended={report.appended} "
            f"duplicates={report.duplicates_skipped} invalid={report.invalid_skipped} "
            f"branch={report.git_branch or '-'} commit={report.git_commit or '-'}"
        )


@main.command("validate-datasets", help="Pydantic-validate dataset files.")
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path, exists=True))
def cmd_validate_datasets(paths: tuple[Path, ...]) -> None:
    targets: list[Path]
    if paths:
        targets = list(paths)
    else:
        targets = sorted(get_settings().absolute_dataset_dir.glob("*.jsonl"))
    if not targets:
        click.echo("no dataset files found")
        sys.exit(0)
    total = 0
    for p in targets:
        n = validate_dataset_file(p)
        click.echo(f"{p.name}: {n} rows ok")
        total += n
    click.echo(f"validated {total} rows across {len(targets)} files")


def _print_result(result: Any) -> None:
    click.echo(f"signature:          {result.signature_name}")
    click.echo(f"artifact_id:        {result.artifact_id}")
    click.echo(f"selected_model:     {result.selected_model}")
    click.echo(f"holdout_accuracy:   {result.holdout_accuracy:.4f}")
    click.echo(f"cost_eur_per_call:  {result.cost_eur_per_call:.6f}")
    click.echo(f"dataset_hash:       {result.dataset_hash}")
    if result.previous_artifact_id:
        click.echo(
            f"previous:           {result.previous_artifact_id} "
            f"(holdout {result.previous_holdout_accuracy:.4f})"
        )
    click.echo("ladder:")
    for c in result.candidates:
        marker = "→" if c.accepted else " "
        click.echo(
            f"  {marker} {c.model:<40} holdout={c.holdout_score:.4f} "
            f"€/call={c.cost_eur_per_call:.6f} obj={c.objective:.4f}"
        )


if __name__ == "__main__":  # pragma: no cover
    main()
