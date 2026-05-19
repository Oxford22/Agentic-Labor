# Agentic Labor

EU-sovereign, Frankfurt-hosted multi-agent back-office automation for Putsch
Mittelstand. Six modules, one workspace, GDPR by construction.

The full architecture, deployment topology, model matrix, and 30-day stand-up
plan live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The integration
layout and dependency graph live in [`docs/INTEGRATION.md`](docs/INTEGRATION.md).

## Workspace layout

```
agentic-labor/
├── pyproject.toml              ← workspace root (uv workspaces)
├── docs/
│   ├── ARCHITECTURE.md         ← system architecture, model matrix, 30-day plan
│   └── INTEGRATION.md          ← workspace layout, dependency graph
├── .github/workflows/ci.yml    ← unified per-package CI matrix
└── packages/
    ├── putsch-obs/             ← observability + eval (Langfuse, OTel) — foundation
    ├── putsch-memory/          ← temporal KG memory (Graphiti + Neo4j) — foundation
    ├── putsch-compile/         ← compiled prompts + routing (DSPy + GEPA + BAML)
    ├── putsch-docs/            ← document/OCR layer (Docling + Qwen-VL)
    └── swarm/                  ← Magentic-One orchestrator on LangGraph
```

## Dependency direction

```
                 swarm  ─────────────┐
                   │                 │
            putsch-docs ──┐          │
                   │     │           │
           putsch-compile │           │
                   │     │           │
                   ▼     ▼           ▼
              putsch-obs        putsch-memory
                         (foundations)
```

The two foundation packages — `putsch-obs` and `putsch-memory` — have no
internal dependencies on the other four. The orchestration tier
(`swarm`) sits on top of every other module. This is the order to bring
modules up in a fresh environment and the order to roll back in an
incident.

## Modules at a glance

| Package | What | Status |
|---|---|---|
| `putsch-obs` | Self-hosted Langfuse + OTel collector + eval CI + WORM-audited PII redaction. The flywheel. | Beta |
| `putsch-memory` | Graphiti on Neo4j 5 Enterprise. Bitemporal facts, no overwrites, deterministic identity. | Beta |
| `putsch-compile` | DSPy signatures + GEPA optimizer + BAML adapter + model registry. Compile-on-PR gate. | Beta |
| `putsch-docs` | Docling + Granite-Docling primary, Qwen2.5-VL-72B fallback, per-field confidence calibration. | Beta |
| `swarm` | Magentic-One orchestrator/worker pattern as a LangGraph state machine. Seven specialists. | Beta |

## Develop

Workspace dev with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --all-packages           # install every package editable + dev deps
uv run pytest packages/          # run the union test suite
uv run --package putsch-obs pytest tests/unit  # one package at a time
```

Or vanilla pip per package:

```bash
pip install -e "packages/putsch-obs[dev]"
pytest packages/putsch-obs/tests
```

## Non-negotiables (enforced across every module)

- Frankfurt VPC, no egress to non-EU endpoints in production paths.
- PII redaction at the SDK boundary, not at the UI. Fail-closed.
- Pydantic v2 for every config, signature, and dataset row.
- Async-safe; bounded queues; instrumentation failure never propagates.
- WORM-enforced audit logs on every personnel-touching read.
- Eval datasets in git; UI-only dataset edits are forbidden.

## License

MIT. See [`LICENSE`](LICENSE).
