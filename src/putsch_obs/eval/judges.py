"""LLM-as-judge harness.

Judge: DeepSeek V3 (MIT, EU-routable via Mistral La Plateforme's third-party
catalogue or Together AI EU). Cheap enough to run on every PR; smart enough
to grade German business text correctly. Temperature 0.0, output forced to
strict JSON.

Rubric library
--------------
The four shipped rubrics correspond 1:1 to the four Putsch flagship
workloads:

* ``invoice_extraction`` — F1 over (rechnung_nr, datum, betrag, lieferant,
  ust_id, positionen). Field-level scoring; rubric is multi-criterion.
* ``mahnung_tone`` — 5-point ordinal scale: aggressive → polite. Rubric
  weights legal correctness 0.6, tone 0.4.
* ``customs_hs`` — exact-match on HS-code (8-digit), partial credit on
  6-digit chapter. The rubric instructions reference EU TARIC where the
  judge can resolve.
* ``datev_booking_code`` — exact-match on SKR-04 booking code, partial on
  account class. We weight precision over recall (an over-booked invoice
  is worse than an under-booked one).

The judge's output is a strict JSON object; we parse and validate against
:class:`Judgement`. Parse errors are surfaced as ``JudgeError``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import httpx
from pydantic import ValidationError

from putsch_obs.config import PutschObsSettings, get_settings
from putsch_obs.eval.schemas import Judgement, Rubric
from putsch_obs.exceptions import JudgeError
from putsch_obs.logging import get_logger

log = get_logger(__name__)


class RubricLibrary:
    """In-process rubric registry. The four shipped rubrics live here."""

    def __init__(self) -> None:
        self._by_id: dict[str, Rubric] = {}
        for r in _SHIPPED_RUBRICS:
            self.register(r)

    def register(self, rubric: Rubric) -> None:
        self._by_id[rubric.rubric_id] = rubric

    def get(self, rubric_id: str) -> Rubric:
        if rubric_id not in self._by_id:
            raise JudgeError(f"unknown rubric: {rubric_id}")
        return self._by_id[rubric_id]

    def all(self) -> Iterable[Rubric]:
        return tuple(self._by_id.values())


# ─────────────────────────────────────────────────────────────────────────────
# Shipped rubrics
# ─────────────────────────────────────────────────────────────────────────────

_INVOICE_EXTRACTION = Rubric(
    rubric_id="invoice_extraction",
    name="Invoice extraction F1",
    instructions=(
        "Du bist ein strenger Prüfer für Rechnungs-Extraktionen. "
        "Vergleiche `actual` mit `expected` Feld für Feld: "
        "`rechnung_nr`, `datum`, `betrag`, `lieferant`, `ust_id`, `positionen`. "
        "Beträge müssen auf den Cent genau übereinstimmen. UST-IdNr Format-tolerant "
        "(Leerzeichen ignorieren). Datum ISO 8601. "
        "Berechne F1 (Precision/Recall) über alle Felder; gewichte `positionen` "
        "als Liste mit Position-level F1. Antwort als strict JSON mit "
        "{score: float in [0,1], pass: bool (>=0.9), rationale: str, "
        "sub_scores: {rechnung_nr,...}, confidence: float}."
    ),
    weights={
        "rechnung_nr": 0.15,
        "datum": 0.10,
        "betrag": 0.25,
        "lieferant": 0.15,
        "ust_id": 0.10,
        "positionen": 0.25,
    },
)

_MAHNUNG_TONE = Rubric(
    rubric_id="mahnung_tone",
    name="Mahnung tone + legal correctness",
    instructions=(
        "Bewerte den Mahnungs-Entwurf nach zwei Achsen: "
        "(a) rechtliche Korrektheit (Mahnstufe, Fristen, BGB-konforme Sprache, "
        "USt-Ausweis) — Gewicht 0.6. "
        "(b) Ton — angemessen geschäftlich, niemals beleidigend, niemals "
        "drohend ausser bei rechtlich gerechtfertigter Inkassoankündigung — "
        "Gewicht 0.4. "
        "Antwort als strict JSON: {score: float in [0,1], pass: bool (>=0.8), "
        "rationale: str (warum), sub_scores: {rechtskonform, ton}, "
        "flagged_for_review: bool (true wenn rechtlich heikel)}."
    ),
    weights={"rechtskonform": 0.6, "ton": 0.4},
)

_CUSTOMS_HS = Rubric(
    rubric_id="customs_hs",
    name="Customs HS-code accuracy",
    instructions=(
        "Vergleiche den vorhergesagten HS-Code (8-stellig) mit dem erwarteten. "
        "Exakt: score 1.0, pass true. "
        "6-stellig Kapitel-Match (erste 6 Ziffern stimmen): score 0.5, pass false. "
        "4-stellig Position-Match: score 0.2, pass false. "
        "Sonst: score 0.0. "
        "Antwort als strict JSON: {score, pass, rationale, sub_scores: "
        "{exact, chapter, position}, confidence}."
    ),
    weights={"exact": 1.0},
)

_DATEV_BOOKING = Rubric(
    rubric_id="datev_booking_code",
    name="DATEV SKR-04 booking-code precision",
    instructions=(
        "Vergleiche die vorhergeschlagene SKR-04 Buchung mit der erwarteten. "
        "Exakter Match (Sollkonto, Habenkonto, Steuerschlüssel): score 1.0, pass true. "
        "Falsche Steuerschlüssel: score 0.3, pass false. "
        "Falsches Konto: score 0.0, pass false. "
        "WICHTIG: gewichte Precision höher als Recall — eine falsche Buchung "
        "ist schlimmer als eine nicht-vorhandene. "
        "Antwort als strict JSON: {score, pass, rationale, sub_scores: "
        "{sollkonto, habenkonto, steuerschluessel}, confidence}."
    ),
    weights={"sollkonto": 0.4, "habenkonto": 0.4, "steuerschluessel": 0.2},
)


_SHIPPED_RUBRICS: tuple[Rubric, ...] = (
    _INVOICE_EXTRACTION,
    _MAHNUNG_TONE,
    _CUSTOMS_HS,
    _DATEV_BOOKING,
)


# ─────────────────────────────────────────────────────────────────────────────
# Judge
# ─────────────────────────────────────────────────────────────────────────────


class LLMJudge:
    """Async LLM judge backed by an OpenAI-compatible endpoint.

    Concurrency is bounded by ``judge_max_concurrency`` in settings —
    larger values risk rate-limiting at La Plateforme, smaller values slow
    eval runs. 4 is a defensible default for a 50-item dataset.
    """

    def __init__(
        self,
        *,
        settings: PutschObsSettings | None = None,
        rubrics: RubricLibrary | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._rubrics = rubrics or RubricLibrary()
        self._client = client
        self._sem = asyncio.Semaphore(self._settings.judge_max_concurrency)

    async def __aenter__(self) -> "LLMJudge":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def judge(
        self,
        *,
        rubric_id: str,
        actual: Any,
        expected: Any | None,
    ) -> Judgement:
        rubric = self._rubrics.get(rubric_id)
        prompt = self._build_prompt(rubric, actual=actual, expected=expected)
        async with self._sem:
            content = await self._call(rubric, prompt)
        return self._parse(content)

    # ── internals ───────────────────────────────────────────────────────

    def _build_prompt(
        self,
        rubric: Rubric,
        *,
        actual: Any,
        expected: Any | None,
    ) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": rubric.instructions},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "actual": actual,
                        "expected": expected,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ]

    async def _call(self, rubric: Rubric, messages: list[dict[str, str]]) -> str:
        assert self._client is not None
        endpoint = str(self._settings.judge_api_base).rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self._settings.judge_model,
            "temperature": self._settings.judge_temperature,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._settings.judge_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            resp = await self._client.post(endpoint, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise JudgeError(f"judge endpoint unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise JudgeError(
                f"judge endpoint returned {resp.status_code}: {resp.text[:400]}"
            )
        try:
            data = resp.json()
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError) as exc:
            raise JudgeError(f"judge endpoint returned malformed payload: {exc}") from exc

    def _parse(self, content: str) -> Judgement:
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise JudgeError(f"judge output is not JSON: {exc}") from exc
        # Normalise `pass` → `pass_` for the Pydantic alias.
        if "pass" in data:
            data["pass"] = bool(data["pass"])
        try:
            return Judgement.model_validate(data)
        except ValidationError as exc:
            raise JudgeError(f"judge output failed schema: {exc}") from exc


__all__ = ["LLMJudge", "Rubric", "RubricLibrary"]
