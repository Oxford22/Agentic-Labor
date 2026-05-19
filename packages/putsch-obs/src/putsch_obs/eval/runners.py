"""Eval runners.

A runner takes an :class:`EvalDataset` and a callable target and produces
an :class:`EvalRunResult`. The runner is responsible for:

* Calling the target once per item (with optional concurrency).
* Capturing latency, cost (from the live OTel span attributes), trace id.
* Invoking the :class:`LLMJudge` per item.
* Writing a Langfuse dataset-run object so the result is browsable.
* Flagging items into the human-review queue if confidence is low.

It is target-agnostic: the target is a ``Callable[[Any], Awaitable[Any]]``
or sync, so a CrewAI crew, a LangGraph compiled graph, or a DSPy program
can all be plugged in identically.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import time
from collections.abc import Callable
from typing import Any

import click

from putsch_obs.eval.datasets import EvalDataset, load_dataset, sync_to_langfuse
from putsch_obs.eval.human_review import HumanReviewQueue
from putsch_obs.eval.judges import LLMJudge, RubricLibrary
from putsch_obs.eval.schemas import (
    AgentTarget,
    EvalItemResult,
    EvalRunResult,
    TargetKind,
)
from putsch_obs.exceptions import JudgeError
from putsch_obs.instrumentation import get_langfuse, get_tracer
from putsch_obs.logging import get_logger

log = get_logger(__name__)


TargetFn = Callable[[Any], Any]


class EvalRunner:
    """Async eval orchestrator. Use as an async context manager."""

    def __init__(
        self,
        *,
        target: AgentTarget,
        target_fn: TargetFn,
        judge: LLMJudge | None = None,
        review_queue: HumanReviewQueue | None = None,
        concurrency: int = 4,
        rubrics: RubricLibrary | None = None,
    ) -> None:
        self._target = target
        self._fn = target_fn
        self._judge = judge or LLMJudge(rubrics=rubrics)
        self._review = review_queue or HumanReviewQueue()
        self._sem = asyncio.Semaphore(concurrency)
        self._tracer = get_tracer("putsch_obs.eval")

    async def __aenter__(self) -> "EvalRunner":
        await self._judge.__aenter__()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._judge.__aexit__(*_)

    async def run(
        self,
        dataset: EvalDataset,
        *,
        baseline: EvalRunResult | None = None,
    ) -> EvalRunResult:
        run = EvalRunResult(
            run_id=secrets.token_urlsafe(8),
            dataset_name=dataset.name,
            dataset_version=dataset.version,
            target=self._target,
        )
        tasks = [self._run_item(item) for item in dataset.items]
        run.items = list(await asyncio.gather(*tasks))
        run.aggregate()

        if baseline is not None:
            run.regression_vs_baseline = round(
                run.mean_score - baseline.mean_score, 4
            )

        await self._write_to_langfuse(dataset, run)
        log.info(
            "eval.run_complete",
            run_id=run.run_id,
            dataset=dataset.name,
            version=dataset.version,
            n=run.n,
            mean_score=run.mean_score,
            pass_rate=run.n_pass / run.n if run.n else 0.0,
            regression=run.regression_vs_baseline,
        )
        return run

    # ── per-item path ───────────────────────────────────────────────────

    async def _run_item(self, item: Any) -> EvalItemResult:
        async with self._sem:
            start = time.perf_counter()
            with self._tracer.start_as_current_span(
                f"eval.item.{item.item_id}"
            ) as sp:
                sp.set_attribute("putsch.kind", "eval_item")
                sp.set_attribute("eval.item_id", item.item_id)
                sp.set_attribute("eval.target", self._target.label())
                try:
                    output = await self._invoke(item.input)
                    err = None
                except Exception as exc:
                    output = None
                    err = f"{type(exc).__name__}: {exc}"
                    sp.record_exception(exc)
                latency_ms = (time.perf_counter() - start) * 1000.0
                sp.set_attribute("putsch.latency_ms", latency_ms)
                cost = self._extract_cost(sp)

                judgement = None
                if err is None and item.rubric_id:
                    try:
                        judgement = await self._judge.judge(
                            rubric_id=item.rubric_id,
                            actual=output,
                            expected=item.expected_output,
                        )
                        sp.set_attribute(
                            "putsch.quality_score", float(judgement.score)
                        )
                        sp.set_attribute("eval.pass", bool(judgement.pass_))
                    except JudgeError as exc:
                        log.warning(
                            "eval.judge_failed",
                            item_id=item.item_id,
                            err=str(exc),
                        )

                trace_id_int = sp.get_span_context().trace_id
                trace_id = format(trace_id_int, "032x") if trace_id_int else None

            result = EvalItemResult(
                item_id=item.item_id,
                target_output=output,
                judgement=judgement,
                latency_ms=latency_ms,
                cost_eur=cost,
                error=err,
                trace_id=trace_id,
            )
            if self._should_flag(result):
                await self._review.flag(result, queue_name=f"{self._target.name}-review")
            return result

    async def _invoke(self, input_value: Any) -> Any:
        result = self._fn(input_value)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _extract_cost(span: Any) -> float | None:
        try:
            attrs = span.attributes or {}
            return attrs.get("gen_ai.usage.cost_eur")  # type: ignore[no-any-return]
        except Exception:
            return None

    @staticmethod
    def _should_flag(result: EvalItemResult) -> bool:
        if result.error is not None:
            return True
        if result.judgement is None:
            return False
        if result.judgement.flagged_for_review:
            return True
        if result.judgement.confidence < 0.7:
            return True
        if not result.judgement.pass_:
            return True
        return False

    # ── Langfuse sync ───────────────────────────────────────────────────

    async def _write_to_langfuse(
        self,
        dataset: EvalDataset,
        run: EvalRunResult,
    ) -> None:
        client = get_langfuse()
        if client is None:
            return
        run_label = f"{self._target.label()}/{run.run_id}"
        for item_result in run.items:
            try:
                client.create_dataset_run_item(
                    dataset_name=dataset.name,
                    run_name=run_label,
                    item_id=item_result.item_id,
                    trace_id=item_result.trace_id,
                    metadata={
                        "score": item_result.judgement.score
                        if item_result.judgement
                        else None,
                        "pass": item_result.judgement.pass_
                        if item_result.judgement
                        else None,
                        "latency_ms": item_result.latency_ms,
                        "cost_eur": item_result.cost_eur,
                        "error": item_result.error,
                        "dataset_version": dataset.version,
                    },
                )
            except Exception as exc:
                log.warning(
                    "eval.langfuse_write_failed",
                    item_id=item_result.item_id,
                    err=str(exc),
                )


async def run_dataset(
    *,
    dataset_name: str,
    target: AgentTarget,
    target_fn: TargetFn,
    concurrency: int = 4,
    sync: bool = True,
) -> EvalRunResult:
    """Convenience entrypoint: load + sync + run."""
    dataset = load_dataset(dataset_name)
    if sync:
        sync_to_langfuse(dataset)
    async with EvalRunner(
        target=target, target_fn=target_fn, concurrency=concurrency
    ) as runner:
        return await runner.run(dataset)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


@click.command(name="putsch-obs-eval")
@click.option("--dataset", required=True, help="Dataset name (without .jsonl)")
@click.option("--target-kind", required=True, type=click.Choice([k.value for k in TargetKind]))
@click.option("--target-name", required=True)
@click.option("--target-version", default="0.0.0")
@click.option("--concurrency", default=4, type=int)
@click.option("--no-sync", is_flag=True, help="Skip Langfuse dataset sync")
@click.option(
    "--target-fn",
    required=True,
    help="Dotted path to a callable, e.g. ap_crew.entrypoints:invoice_extractor",
)
def cli(
    dataset: str,
    target_kind: str,
    target_name: str,
    target_version: str,
    concurrency: int,
    no_sync: bool,
    target_fn: str,
) -> None:
    """Run an eval set against a target callable."""
    import importlib

    module_path, _, fn_name = target_fn.partition(":")
    if not module_path or not fn_name:
        raise click.BadParameter("target-fn must be of the form module.path:callable")
    module = importlib.import_module(module_path)
    fn = getattr(module, fn_name)

    target = AgentTarget(
        kind=TargetKind(target_kind),
        name=target_name,
        version=target_version,
    )
    result = asyncio.run(
        run_dataset(
            dataset_name=dataset,
            target=target,
            target_fn=fn,
            concurrency=concurrency,
            sync=not no_sync,
        )
    )
    click.echo(result.model_dump_json(indent=2))


__all__ = ["EvalRunner", "run_dataset", "cli"]
