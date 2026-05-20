# Prioritization

How we decide what to work on next in Agentic-Labor. Versioned so the rubric is auditable and so the backlog reads as a single, scannable document instead of a Notion graveyard.

## TL;DR

Every candidate work item gets a score: `(Impact * Confidence * Leverage) / Effort`. Highest score wins. Ties broken by (1) live-context proximity, (2) reversibility, (3) learning value. Items that can't be scored are sent back for sharpening.

## The rubric

**Impact (1-5)** - how much does shipping this move the system? A 5 unblocks other modules or removes a class of bug. A 1 is a polish item only the author will notice. The two foundation packages (`putsch-obs`, `putsch-memory`) get a default +1 because they sit under everything else; touching them propagates.

**Confidence (0.1-1.0)** - probability the work, as scoped, actually delivers the claimed impact. Most "this will be great" ideas live at 0.3. Items with a working spike or a passing test sit at 0.8+. Items that depend on a vendor wheel landing get capped at 0.5 until the wheel is in `uv.lock`.

**Leverage (1-3)** - compounding factor. 1 = one-shot fix. 2 = touches a shared abstraction so future work gets easier. 3 = changes the CI / build / fixture / contract surface that every other item rides on. Don't inflate this; if everything is a 3, nothing is.

**Effort (hours)** - calendar hours of focused work to a mergeable PR with tests. Round to the nearest hour, minimum 1. If you can't estimate, the item is too vague - send it back.

Score = `(I * C * L) / E`. Above 2.0 is "do now." 1.0-2.0 is "do this week." Below 1.0 is "park or drop."

## Tiebreakers

1. **Live-context proximity.** Work adjacent to the PR or incident in front of us today beats equally-scored work in some other module. Switching cost is a real tax.
2. **Reversibility.** Migrations, schema changes, and deletions lose to additive changes at equal score - we can't unship a deleted table.
3. **Learning value.** Where two items still tie, pick the one that teaches us something we don't already know (a new failure mode, a new tool, a new vendor's behavior under load).

A fourth, unwritten tiebreaker: if you've been staring at the table for more than five minutes, just pick one and start.

## How to use this document

**Intake.** Anything that isn't yet ready to be a GitHub issue lands at the bottom of the "Backlog" table with rough I/C/L/E numbers. No estimate, no row. The intake step forces the work to become legible before it competes for attention.

**Promotion.** When a row scores above 1.0 and has a clear acceptance criterion, open a GitHub issue and put the issue number in the `Source` column. The row stays in the table - the doc is the ledger, Issues is the work queue, and inline `# TODO(owner, #issue):` comments are the breadcrumbs in code. Anything that doesn't have one of those three homes gets deleted.

**Closure.** Shipped items move to the "Archive" section at the bottom with the PR number and one-line outcome. Dropped items move there too, with a one-line reason. The archive is not optional - it's how we tell whether the rubric is actually predicting reality.

**Re-scoring.** Re-score the open table when (a) a foundation module changes shape, (b) a vendor announces something that breaks an assumption, or (c) we close five items in a row. Otherwise the numbers drift.

## Backlog

| # | Item | Source | I | C | L | E | Score | Status | Notes |
|---|------|--------|---|---|---|---|-------|--------|-------|
| 1 | Fix `test_redaction_properties.py` Hypothesis health-check failures | PR #8 | 4 | 1.0 | 2 | 1 | 8.0 | in-flight | Add `HealthCheck.function_scoped_fixture` to the two `@settings` decorators that don't yet have it. Foundation module (+1 to impact). |
| 2 | Fix `test_pii_redaction_in_attributes` OTel global-provider collision | PR #8 | 4 | 0.9 | 2 | 2 | 3.6 | in-flight | Scope a fresh `TracerProvider` per test or attach a second `SimpleSpanProcessor` to the existing provider; the current fixture loses to OTel's "no override" guard on the second test. |
| 3 | Auto-mark `tests/integration/**` so CI's `-m "not integration"` filter actually filters | PR #8 | 4 | 1.0 | 3 | 1 | 12.0 | in-flight | Add `pytest_collection_modifyitems` to `tests/integration/conftest.py` (obs) and equivalent for memory. Right now the filter is a no-op because the files lack the marker. Highest leverage item on the board. |
| 4 | Move `--cov-fail-under=85` out of `putsch-memory`'s `addopts` | PR #8 | 5 | 1.0 | 3 | 1 | 15.0 | in-flight | Hardcoding 85 in `addopts` means every PR run that excludes `integration` and `chaos` fails on coverage. Move it to a workflow flag or a nightly job. Currently blocks the entire memory matrix. |
| 5 | Fix `memory_client` async-generator fixture in `putsch-memory/tests/conftest.py` | PR #8 | 5 | 0.8 | 3 | 3 | 4.0 | in-flight | Declared with `@pytest.fixture` but `yield`s and `await`s - pytest-asyncio's auto mode trips on the fixture-runner scope. Switch to `@pytest_asyncio.fixture` and fix the `fake_driver.close` coroutine-at-creation bug. |
| 6 | Split `putsch-memory/tests` into `unit/` and `integration/` subdirs | PR #8 follow-up | 3 | 0.9 | 2 | 2 | 2.7 | open | Flat layout makes the marker filter the only gate. Subdir layout + auto-mark gives belt-and-braces and lets the workflow target `tests/unit/` directly. |
| 7 | Pin pytest-asyncio version across all packages | new | 3 | 0.9 | 3 | 1 | 8.1 | open | The setup-time errors smell like API drift between minor versions. Pin in workspace root and verify each package's `dev` extra picks the same one. |
| 8 | Frankfurt egress allow-list enforcement (housekeeping) | inferred from README | 5 | 0.4 | 3 | 8 | 0.75 | parked | Non-negotiable per the README but no scoped issue yet. Park until someone writes the acceptance test. |
| 9 | WORM audit-log writer end-to-end on personnel-touching reads | inferred from README | 5 | 0.5 | 3 | 6 | 1.25 | open | Pair with item 8. Acceptance: an unredacted read of a PII span is impossible to make without a corresponding audit row. |
| 10 | `putsch-obs[memory]` extra wiring sanity check | inferred from extras | 3 | 0.8 | 1 | 1 | 2.4 | open | The README's "broken wheel doesn't take down install" claim needs a CI test that imports each extra in isolation. |
| 11 | This document | PR (this one) | 2 | 1.0 | 3 | 1 | 6.0 | shipping | Rubric + ledger. Meta, but real. |

## Archive

(Items move here once shipped or explicitly dropped. Format: `[date] [PR/issue] - one-line outcome or reason for drop.`)

## Notes on income-generating work

Income items aren't a separate category - they go through the same rubric. The honest reason most "this will make money" ideas die in the backlog is that they have high claimed Impact, an unverified Confidence (0.2-0.3 is realistic), and an Effort estimate that ignores integration and support. The rubric forces those numbers into the open. If you have an income-generating idea that won't score above 1.0 with honest numbers, the right answer is usually to either (a) cut scope until Effort comes down, (b) run a one-day spike to lift Confidence, or (c) drop it.

The only standing exception: items that protect existing income (GDPR posture, EU-sovereignty, audit-log integrity) get scored with Confidence floored at 0.7 because the downside of being wrong is asymmetric. Don't abuse that floor.

## Maintenance

This document is the source of truth. If you add an inline `# TODO` in code, it needs an issue number and the issue needs a row here. If a row sits at "open" for more than 30 days without being touched, it's drifting - either re-score it or move it to Archive with a drop reason. Stale prioritization is worse than no prioritization, because it pretends to be a plan.
