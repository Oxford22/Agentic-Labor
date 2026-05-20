# AGENTS.md

## Cursor Cloud specific instructions

This is a pure Python library (no web server, no Docker, no database). The only runtime requirement is Python >= 3.10.

### Quick reference

| Action | Command |
|--------|---------|
| Install deps | `pip install -e ".[dev]"` |
| Run tests | `pytest` |
| Lint | `ruff check .` |
| Build graph | `from swarm import build_graph` (import-time check) |

### Key architecture notes

- **Tests use `StubModel`** (in `tests/conftest.py`) with canned JSON responses. No LLM API key is needed to run the full test suite.
- **Examples require an OpenAI-compatible endpoint** (`OPENAI_API_KEY` env var) via `langchain-openai`. Only needed for `examples/` scripts, never for `pytest`.
- The package is installed in editable mode (`pip install -e ".[dev]"`). Hot-reload is not applicable since there is no running server—just re-run `pytest` or your script after changes.
- `ruff check .` reports 2 pre-existing unused-import warnings in tests; these are not regressions.
- The LangGraph wiring lives in `swarm/graph.py`; import `build_graph` from `swarm` to compile the state graph.
- Trust/injection-defense layer (`trust/`) wraps all untrusted content in `<external_content>` envelopes. Tests in `tests/test_injection_defense.py` cover this.
