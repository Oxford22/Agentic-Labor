"""Verzeichnis von Verarbeitungstätigkeiten generator.

Produces:

* ``verzeichnis.yaml`` — machine-readable, used by the deploy pipeline to
  emit ClickHouse TTL settings keyed by retention class.
* ``verzeichnis.md`` — human-readable, ready for hand-off to the
  Datenschutzbeauftragte.

CLI: ``putsch-obs-dsgvo-emit --out docs/verzeichnis``.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Iterable

import click
import yaml

from putsch_obs.dsgvo.registry import ProcessingActivity, registered_activities
from putsch_obs.logging import get_logger

log = get_logger(__name__)


_MARKDOWN_HEADER = """\
# Verzeichnis von Verarbeitungstätigkeiten (Art. 30 DSGVO)

**Verantwortlicher**: Putsch GmbH & Co. KG, Hagen, Nordrhein-Westfalen, Deutschland.

**Stand**: {today}

**Datenschutzbeauftragte**: datenschutz@putsch.de

> Dieses Dokument wird automatisch aus dem Service-Registry generiert.
> Manuelle Änderungen werden beim nächsten Lauf überschrieben. Pflege
> erfolgt über ``putsch_obs.dsgvo.register_service`` in der jeweiligen
> Service-Codebasis.
"""


def generate_verzeichnis(
    activities: Iterable[ProcessingActivity] | None = None,
) -> str:
    """Render the Verzeichnis as Markdown."""
    acts = list(activities) if activities is not None else list(registered_activities())
    acts.sort(key=lambda a: a.service_name)
    lines: list[str] = [_MARKDOWN_HEADER.format(today=date.today().isoformat()), ""]
    for a in acts:
        lines.append(f"## {a.bezeichnung} ({a.service_name})")
        lines.append("")
        lines.append(f"- **Zweck**: {a.zweck}")
        lines.append(f"- **Rechtsgrundlage**: {a.rechtsgrundlage.value}")
        lines.append(
            "- **Betroffene Personen**: "
            + ", ".join(p.value for p in a.betroffene_personen)
        )
        lines.append(
            "- **Datenkategorien**: "
            + ", ".join(c.value for c in a.datenkategorien)
        )
        lines.append("- **Empfänger**: " + ", ".join(a.empfaenger))
        lines.append(
            "- **Drittlandstransfers**: "
            + (", ".join(a.drittland_transfers) if a.drittland_transfers else "keine")
        )
        lines.append(f"- **Aufbewahrungsfrist**: {a.aufbewahrungsfrist}")
        lines.append(f"- **AI-Act-Risikoklasse**: {a.ai_act_risk_class}")
        lines.append(
            "- **Technische & organisatorische Maßnahmen**:\n  - "
            + "\n  - ".join(a.technische_maßnahmen)
        )
        lines.append("")
    return "\n".join(lines)


def generate_yaml(
    activities: Iterable[ProcessingActivity] | None = None,
) -> str:
    """Render the Verzeichnis as YAML (machine-readable)."""
    acts = list(activities) if activities is not None else list(registered_activities())
    payload = {
        "stand": date.today().isoformat(),
        "verantwortlicher": "Putsch GmbH & Co. KG",
        "aktivitaeten": [a.model_dump(mode="json") for a in acts],
    }
    return yaml.safe_dump(payload, sort_keys=True, allow_unicode=True)


@click.command(name="putsch-obs-dsgvo-emit")
@click.option(
    "--out",
    type=click.Path(file_okay=False, dir_okay=True),
    default="docs/verzeichnis",
    help="Output directory.",
)
@click.option(
    "--import-modules",
    multiple=True,
    help=(
        "Dotted import paths whose import causes services to register "
        "themselves. Repeatable."
    ),
)
def cli(out: str, import_modules: tuple[str, ...]) -> None:
    """Emit the DSGVO Verzeichnis for all registered services."""
    import importlib

    for mod in import_modules:
        try:
            importlib.import_module(mod)
        except ImportError as exc:
            raise click.ClickException(f"could not import {mod!r}: {exc}") from exc
    out_path = Path(out)
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "verzeichnis.md").write_text(generate_verzeichnis(), encoding="utf-8")
    (out_path / "verzeichnis.yaml").write_text(generate_yaml(), encoding="utf-8")
    os.chmod(out_path / "verzeichnis.md", 0o644)
    os.chmod(out_path / "verzeichnis.yaml", 0o644)
    click.echo(f"wrote {out_path/'verzeichnis.md'} and {out_path/'verzeichnis.yaml'}")


__all__ = ["generate_verzeichnis", "generate_yaml", "cli"]
