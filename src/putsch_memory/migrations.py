"""Migration runner — generates Cypher DDL from the ontology and applies it.

Constraints are derived from `BUSINESS_GRAPH.cypher_constraints()`, not
hand-written. This guarantees the schema in Neo4j and the schema in
Python types stay in lock-step. The only "manual" bit is the version
table — `_PutschMigration` — which tracks which migration was applied
last, so re-running is a no-op.

For *destructive* migrations (renaming a label, dropping an attribute),
we still require hand-written Cypher in `migrations/_destructive/`.
Those are out of scope here on purpose: any destructive migration
warrants its own review, not an automated pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import sys

from putsch_memory.config import settings as default_settings
from putsch_memory.graphiti_client import MemoryClient
from putsch_memory.logging import configure_logging, get_logger
from putsch_memory.ontology import BUSINESS_GRAPH

logger = get_logger(__name__)


async def up(client: MemoryClient) -> None:
    constraints = BUSINESS_GRAPH.cypher_constraints()
    fingerprint = _fingerprint(constraints)
    applied = await _latest_fingerprint(client)
    if applied == fingerprint:
        logger.info("migrations_up_to_date", fingerprint=fingerprint)
        return
    for cypher in constraints:
        await client._run_cypher(cypher, {}, fetch=False)  # noqa: SLF001
    await _record_application(client, fingerprint, len(constraints))
    logger.info(
        "migrations_applied",
        fingerprint=fingerprint,
        constraints=len(constraints),
    )


async def status(client: MemoryClient) -> dict[str, object]:
    constraints = BUSINESS_GRAPH.cypher_constraints()
    fingerprint = _fingerprint(constraints)
    applied = await _latest_fingerprint(client)
    return {
        "ontology_fingerprint": fingerprint,
        "applied_fingerprint": applied,
        "up_to_date": fingerprint == applied,
        "constraint_count": len(constraints),
    }


def _fingerprint(constraints: list[str]) -> str:
    return hashlib.sha256("\n".join(constraints).encode()).hexdigest()[:16]


async def _latest_fingerprint(client: MemoryClient) -> str | None:
    rows = await client._run_cypher(  # noqa: SLF001
        """
        MATCH (m:_PutschMigration)
        RETURN m.fingerprint AS fp
        ORDER BY m.applied_at DESC
        LIMIT 1
        """,
        {},
    )
    return rows[0]["fp"] if rows else None


async def _record_application(client: MemoryClient, fingerprint: str, count: int) -> None:
    await client._run_cypher(  # noqa: SLF001
        """
        CREATE (m:_PutschMigration {
          fingerprint: $fp,
          applied_at: datetime($now),
          constraints: $n
        })
        """,
        {"fp": fingerprint, "now": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(), "n": count},
        fetch=False,
    )


def cli(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="putsch-memory-migrate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    async def _main() -> int:
        async with await MemoryClient.from_env(default_settings) as client:
            if args.cmd == "up":
                await up(client)
                return 0
            if args.cmd == "status":
                print(await status(client))
                return 0
        return 1

    return asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
