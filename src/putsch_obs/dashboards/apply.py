"""Apply code-defined dashboards to a Langfuse instance.

Idempotent: a dashboard with a matching ``slug`` is updated in place,
otherwise it's created. We talk to the Langfuse REST API directly because
the SDK's dashboard surface is still in flux at v2.

CLI:

    putsch-obs-dashboards-apply           # apply all
    putsch-obs-dashboards-apply --only putsch_ap_kpis

Failure semantics
-----------------
This is operator-facing tooling, not the production hot path.
``DashboardApplyError`` is raised on first failure; the caller sees the
full HTTP response. There's no "best effort" mode — a half-applied
dashboard set is a worse outcome than no apply.
"""

from __future__ import annotations

import json
from base64 import b64encode
from pathlib import Path

import click
import httpx

from putsch_obs.config import get_settings
from putsch_obs.exceptions import DashboardApplyError
from putsch_obs.logging import get_logger

log = get_logger(__name__)

DASHBOARD_DIR = Path(__file__).parent


def _auth_header() -> dict[str, str]:
    cfg = get_settings()
    pk = cfg.langfuse_public_key.get_secret_value()
    sk = cfg.langfuse_secret_key.get_secret_value()
    if not (pk and sk):
        raise DashboardApplyError("Langfuse credentials missing")
    creds = b64encode(f"{pk}:{sk}".encode()).decode("ascii")
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


def _load_specs(only: str | None = None) -> list[dict[str, object]]:
    specs: list[dict[str, object]] = []
    for path in sorted(DASHBOARD_DIR.glob("*.json")):
        slug = path.stem
        if only and slug != only:
            continue
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DashboardApplyError(f"{path}: invalid JSON: {exc}") from exc
        spec.setdefault("slug", slug)
        specs.append(spec)
    if only and not specs:
        raise DashboardApplyError(f"no dashboard with slug {only!r}")
    return specs


def apply(only: str | None = None) -> list[str]:
    """Upsert dashboards into Langfuse. Returns slugs applied."""
    cfg = get_settings()
    base = str(cfg.langfuse_host).rstrip("/")
    headers = _auth_header()
    applied: list[str] = []
    with httpx.Client(timeout=20.0) as client:
        for spec in _load_specs(only):
            slug = str(spec["slug"])
            url = f"{base}/api/public/dashboards"
            # Best-effort PATCH-by-slug, then POST if not found.
            patch = client.patch(
                f"{url}/{slug}", headers=headers, content=json.dumps(spec)
            )
            if patch.status_code == 404:
                resp = client.post(url, headers=headers, content=json.dumps(spec))
            else:
                resp = patch
            if resp.status_code >= 400:
                raise DashboardApplyError(
                    f"dashboard {slug!r} apply failed: "
                    f"HTTP {resp.status_code} — {resp.text[:400]}"
                )
            applied.append(slug)
            log.info(
                "dashboards.applied",
                slug=slug,
                method=resp.request.method,
                status=resp.status_code,
            )
    return applied


@click.command(name="putsch-obs-dashboards-apply")
@click.option("--only", default=None, help="Apply only a specific dashboard slug.")
def cli(only: str | None) -> None:
    """Apply code-defined dashboards to Langfuse."""
    try:
        applied = apply(only)
    except DashboardApplyError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"applied: {', '.join(applied)}")


__all__ = ["DASHBOARD_DIR", "apply", "cli"]
