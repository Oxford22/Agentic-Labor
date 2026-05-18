"""Top-level CLI: ``putsch-obs-init``.

Bootstraps a fresh service: writes a ``.env`` from the template, prints the
recommended ``init()`` snippet, validates the local Langfuse endpoint is
reachable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click
import httpx

from putsch_obs.config import get_settings
from putsch_obs.logging import get_logger

log = get_logger(__name__)


@click.command(name="putsch-obs-init")
@click.option("--target", default=".", help="Target directory to bootstrap.")
def main(target: str) -> None:
    """Bootstrap putsch-obs for a service in `target`."""
    target_path = Path(target).resolve()
    template = (
        Path(__file__).resolve().parent.parent.parent
        / "deploy"
        / "langfuse"
        / ".env.template"
    )
    dest = target_path / ".env"
    if not dest.exists() and template.exists():
        shutil.copy(template, dest)
        click.echo(f"wrote {dest}")
    elif dest.exists():
        click.echo(f".env already present at {dest}; skipping copy")
    cfg = get_settings()
    click.echo(f"service_name: {cfg.service_name}")
    click.echo(f"env: {cfg.deployment_environment}")
    click.echo(f"langfuse: {cfg.langfuse_host}")
    try:
        resp = httpx.get(
            str(cfg.langfuse_host).rstrip("/") + "/api/public/health", timeout=3.0
        )
        click.echo(f"langfuse /health: HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        click.echo(f"langfuse /health: unreachable ({exc})", err=True)
    click.echo(
        "\nNext step: in your service's entrypoint:\n"
        "    from putsch_obs import init\n"
        "    init(service_name='my-service')\n"
    )


if __name__ == "__main__":
    main()
