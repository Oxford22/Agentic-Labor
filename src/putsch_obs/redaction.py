"""PII redaction.

This is the highest-stakes module in the package. Read carefully.

Threat model
------------
We are exporting span attributes — including LLM prompts and outputs — to a
Langfuse server. Even though that server is self-hosted in the Putsch
Frankfurt VPC, the Betriebsrat and the Datenschutzbeauftragte require that
any traced payload pass through a redaction stage *before* leaving the
service that emitted it. The data must never sit on disk, or in a ClickHouse
table, in unredacted form unless an authorized auditor explicitly
un-redacts it via the vault (which is itself audited).

Design
------
The engine is a two-stage pipeline:

1. **Deterministic stage**. Regex-based for high-confidence patterns:
   IBAN, USt-IdNr, Steuernummer, German Personalausweis numbers,
   emails, German phone numbers, BIC, SEPA mandate IDs. Each match is
   replaced with an opaque token of the form ``<<PII:{type}:{token}>>``
   where ``{token}`` is a 16-char URL-safe random string. The mapping
   ``token → original`` is written to the vault.

2. **LLM stage**. For free-form German text (customer names, addresses
   in invoice line items, Mahnung greetings), the deterministic stage
   leaves them through. The LLM stage uses Qwen3-14B running locally
   (no external network) to extract residual entities. Matches go
   through the same tokenization path.

Failure mode
------------
Both stages fail-closed. If the LLM endpoint times out, we raise
``RedactionError`` and the caller drops the span. This is the right
trade-off: a lost trace is a missed feature; a leaked tax ID is a
notifiable GDPR incident.

Reversibility
-------------
Tokens are reversible only via the vault, which holds:

* The token → original mapping, Fernet-encrypted at rest.
* An append-only audit log of every un-redaction call (who, why, when),
  hash-chained per row.

Un-redaction is an explicit operator action; the SDK never un-redacts
implicitly.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Iterable

import httpx

from putsch_obs.config import PutschObsSettings, RedactionMode, get_settings
from putsch_obs.exceptions import RedactionError
from putsch_obs.logging import get_logger

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# PII categories. Mirror the labels we attach to vault rows and audit entries.
# ─────────────────────────────────────────────────────────────────────────────
class PIICategory(StrEnum):
    IBAN = "iban"
    BIC = "bic"
    USTID = "ust_id"             # USt-IdNr (DE\d{9})
    STEUER_NR = "steuer_nr"      # German Steuernummer (regional formats)
    PERSO_ID = "perso_id"        # Personalausweisnummer
    EMAIL = "email"
    PHONE = "phone_de"
    SEPA_MANDATE = "sepa_mandate"
    NAME = "name"                # LLM-extracted
    ADDRESS = "address"          # LLM-extracted
    CUSTOM = "custom"


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic patterns.
#
# Rules of thumb when editing this section:
#
#  * Anchor on word boundaries / negative lookaround. We must not eat the
#    leading "DE" of an unrelated word.
#  * Prefer broader-then-validate. The IBAN regex matches DE + 20 digits
#    with optional spaces; ``_iban_is_valid`` runs mod-97.
#  * NEVER use ``re.search`` inside callable patterns — pre-compile only.
# ─────────────────────────────────────────────────────────────────────────────

# DE IBAN: "DE" + 2 check + 8 BLZ + 10 account, allow grouped-by-4 spaces.
# Use a non-capturing group so finditer reports the whole match.
_IBAN_RE: Final = re.compile(
    r"(?<![A-Z0-9])DE\d{2}(?:\s?\d{4}){4}\s?\d{2}(?![A-Z0-9])",
    re.IGNORECASE,
)
# USt-IdNr: DE + 9 digits.
_USTID_RE: Final = re.compile(r"(?<![A-Z0-9])DE\s?\d{9}(?![A-Z0-9])", re.IGNORECASE)
# Steuernummer — three common regional formats. Hard to nail exactly, so we
# match the general shape and let the LLM stage catch outliers.
_STEUERNR_RE: Final = re.compile(
    r"(?<!\d)(?:\d{2,3}/\d{3,4}/\d{4,5}|\d{3,4}/\d{4,5})(?!\d)"
)
# Personalausweis. Two shapes coexist in the wild:
#   * pre-2010 (still valid until 2031): [A-Z]\d{8}[A-Z]\d         (11 chars)
#     e.g. T22000129K2 — letter + 8 digits + letter + check digit
#   * post-2010 new card:                [A-Z][A-Z0-9]{8}\d        (10 chars)
#     e.g. L01X00T479 — letter + 8 alphanumeric + check digit
# We OR them so both formats get redacted.
_PERSO_ID_RE: Final = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z]\d{8}[A-Z]\d|[A-Z][A-Z0-9]{8}\d)(?![A-Z0-9])"
)
# BIC: 8 or 11 chars, ISO 9362.
_BIC_RE: Final = re.compile(r"(?<![A-Z0-9])[A-Z]{4}DE[A-Z0-9]{2}(?:[A-Z0-9]{3})?(?![A-Z0-9])")
# Email — RFC-pragmatic, not RFC-perfect; sufficient for redaction. The
# trailing lookahead intentionally excludes `\w` and `-` only (NOT `.`) so
# an email at sentence-end like "schick an x@y.com." still matches.
_EMAIL_RE: Final = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w-])"
)
# German phone: +49 / 0049 / 0 prefix, then 3-5 digit area code, then 4-12 digits.
# Allow spaces, hyphens, slashes, and parens (common in DIN 5008 formats:
# "(089) 1234 567"). Parens are added to the separator class — they cannot
# anchor a match alone because the leading alternation still requires the
# +49 / 0049 / 0 prefix.
_PHONE_RE: Final = re.compile(
    r"(?<![\d+])(?:\+49|0049|0)[\s\-/()]?(?:\d[\s\-/()]?){8,14}\d(?!\d)"
)
# SEPA mandate id: prefix "PUTSCH-" or arbitrary, followed by alnum/-.
_SEPA_RE: Final = re.compile(
    r"(?<![A-Z0-9])MANDATE-[A-Z0-9]{6,32}(?![A-Z0-9])", re.IGNORECASE
)


_DETERMINISTIC_PATTERNS: Final[tuple[tuple[PIICategory, re.Pattern[str]], ...]] = (
    # Order matters: longer/more-specific first so they win the substring fight.
    (PIICategory.IBAN, _IBAN_RE),
    (PIICategory.BIC, _BIC_RE),
    (PIICategory.USTID, _USTID_RE),
    (PIICategory.STEUER_NR, _STEUERNR_RE),
    (PIICategory.PERSO_ID, _PERSO_ID_RE),
    (PIICategory.SEPA_MANDATE, _SEPA_RE),
    (PIICategory.EMAIL, _EMAIL_RE),
    (PIICategory.PHONE, _PHONE_RE),
)


def _iban_is_valid(iban: str) -> bool:
    """Mod-97 check on a DE IBAN (ISO 13616). Tolerates whitespace."""
    s = re.sub(r"\s+", "", iban).upper()
    if len(s) != 22 or not s.startswith("DE"):
        return False
    # Move first 4 to end, replace letters with numbers (A=10, B=11, …).
    rearranged = s[4:] + s[:4]
    numeric: list[str] = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        elif "A" <= ch <= "Z":
            numeric.append(str(ord(ch) - 55))
        else:
            return False
    return int("".join(numeric)) % 97 == 1


# ─────────────────────────────────────────────────────────────────────────────
# Vault protocol. The redactor stays decoupled from Postgres so tests can
# substitute a memory-backed vault.
# ─────────────────────────────────────────────────────────────────────────────


class VaultProtocol:
    """Minimal interface the redactor depends on. See ``putsch_obs.vault``."""

    def store(  # pragma: no cover - protocol
        self,
        token: str,
        category: PIICategory,
        original: str,
        *,
        context_hint: str | None = None,
    ) -> None:
        ...

    async def store_async(  # pragma: no cover - protocol
        self,
        token: str,
        category: PIICategory,
        original: str,
        *,
        context_hint: str | None = None,
    ) -> None:
        ...


class _NullVault:
    """No-op vault used when ``RedactionMode.OFF`` or in pure unit tests.

    Storing nothing means un-redaction is impossible, which is the correct
    posture for development environments.
    """

    def store(
        self,
        token: str,
        category: PIICategory,
        original: str,
        *,
        context_hint: str | None = None,
    ) -> None:
        return None

    async def store_async(
        self,
        token: str,
        category: PIICategory,
        original: str,
        *,
        context_hint: str | None = None,
    ) -> None:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class RedactionResult:
    """Returned by :meth:`RedactionEngine.redact`.

    ``redacted`` is the safe-to-export string.

    ``mapping`` is the in-memory ``token → (category, original)`` mapping,
    available to the caller in case it must be persisted in a *different*
    store than the vault. The vault is also written to as a side effect.
    """

    redacted: str
    mapping: Mapping[str, tuple[PIICategory, str]] = field(default_factory=dict)
    deterministic_hits: int = 0
    llm_hits: int = 0

    @property
    def any_hits(self) -> bool:
        return self.deterministic_hits + self.llm_hits > 0


def _make_token() -> str:
    return secrets.token_urlsafe(12)


def _wrap_token(category: PIICategory, token: str) -> str:
    return f"<<PII:{category.value}:{token}>>"


class RedactionEngine:
    """Two-stage redactor. Use the singleton from ``get_engine()`` in production."""

    def __init__(
        self,
        settings: PutschObsSettings | None = None,
        vault: VaultProtocol | None = None,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._vault: VaultProtocol = vault or _NullVault()
        self._http = http  # injected for testing
        self._mode = self._settings.redaction_mode

    # ── deterministic stage ──────────────────────────────────────────────

    def _redact_deterministic(
        self,
        text: str,
        mapping: dict[str, tuple[PIICategory, str]],
    ) -> tuple[str, int]:
        if not text:
            return text, 0
        hits = 0
        out = text
        for category, pattern in _DETERMINISTIC_PATTERNS:
            def _sub(m: re.Match[str], _cat: PIICategory = category) -> str:
                nonlocal hits
                original = m.group(0)
                if _cat is PIICategory.IBAN and not _iban_is_valid(original):
                    # Looks like an IBAN but fails mod-97. Still redact: the
                    # cost of leaking a non-IBAN string that LOOKS like an
                    # IBAN is zero; the cost of leaking a malformed IBAN is
                    # an audit finding.
                    pass
                token = _make_token()
                wrapped = _wrap_token(_cat, token)
                mapping[token] = (_cat, original)
                hits += 1
                return wrapped

            out = pattern.sub(_sub, out)
        return out, hits

    # ── LLM stage ────────────────────────────────────────────────────────

    async def _redact_llm(
        self,
        text: str,
        mapping: dict[str, tuple[PIICategory, str]],
    ) -> tuple[str, int]:
        if self._mode is RedactionMode.DETERMINISTIC_ONLY:
            return text, 0
        if not text.strip():
            return text, 0

        client = self._http or httpx.AsyncClient(
            timeout=self._settings.redaction_llm_timeout_seconds,
        )
        endpoint = str(self._settings.redaction_llm_endpoint).rstrip("/") + "/chat/completions"
        payload = {
            "model": "qwen3-14b-redactor",
            "temperature": 0.0,
            "messages": [
                {
                    "role": "system",
                    "content": _LLM_SYSTEM_PROMPT,
                },
                {"role": "user", "content": text},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._settings.redaction_llm_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            try:
                resp = await client.post(endpoint, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            finally:
                if self._http is None:
                    await client.aclose()
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
            # Fail-closed: the caller must drop the span.
            log.warning(
                "redaction.llm_failed",
                err=str(exc),
                err_type=type(exc).__name__,
            )
            raise RedactionError(
                "LLM redactor unavailable; refusing to export possibly-PII payload"
            ) from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RedactionError(f"LLM redactor returned malformed payload: {exc}") from exc

        # The LLM emits a JSON list of {span: str, category: str}. We do the
        # replacement here so we control tokenization. Tolerate cases where
        # the LLM returned nothing — that is also valid (no PII detected).
        import json

        try:
            spans = json.loads(content) if content.strip() else []
        except json.JSONDecodeError as exc:
            raise RedactionError(f"LLM redactor returned non-JSON: {exc}") from exc

        if not isinstance(spans, list):
            raise RedactionError("LLM redactor returned non-list payload")

        hits = 0
        out = text
        # Replace longest-first to avoid nested replacements.
        spans_sorted = sorted(spans, key=lambda s: -len(s.get("span", "")))
        for entry in spans_sorted:
            span = entry.get("span")
            cat_raw = entry.get("category", "custom")
            if not span or not isinstance(span, str):
                continue
            try:
                cat = PIICategory(cat_raw)
            except ValueError:
                cat = PIICategory.CUSTOM
            if span not in out:
                continue
            token = _make_token()
            wrapped = _wrap_token(cat, token)
            mapping[token] = (cat, span)
            # Replace ONLY the first occurrence per LLM-flagged span; the LLM
            # is responsible for emitting one entry per occurrence.
            out = out.replace(span, wrapped, 1)
            hits += 1
        return out, hits

    # ── public API ───────────────────────────────────────────────────────

    def redact(self, text: str) -> RedactionResult:
        """Synchronous redaction. Skips the LLM stage.

        Use this in OTel processors that run on the export hot path. The
        async path (:meth:`redact_async`) is for offline jobs and tests.
        """
        if self._mode is RedactionMode.OFF:
            return RedactionResult(redacted=text)
        mapping: dict[str, tuple[PIICategory, str]] = {}
        redacted, det_hits = self._redact_deterministic(text, mapping)
        for token, (cat, original) in mapping.items():
            self._vault.store(token, cat, original)
        return RedactionResult(
            redacted=redacted,
            mapping=mapping,
            deterministic_hits=det_hits,
        )

    async def redact_async(self, text: str) -> RedactionResult:
        """Full two-stage redaction. Use offline / in eval pipelines.

        Raises :class:`RedactionError` if the LLM stage fails. The caller
        MUST handle this by dropping the payload, not by retrying.
        """
        if self._mode is RedactionMode.OFF:
            return RedactionResult(redacted=text)
        mapping: dict[str, tuple[PIICategory, str]] = {}
        redacted, det_hits = self._redact_deterministic(text, mapping)
        redacted, llm_hits = await self._redact_llm(redacted, mapping)
        for token, (cat, original) in mapping.items():
            await self._vault.store_async(token, cat, original)
        return RedactionResult(
            redacted=redacted,
            mapping=mapping,
            deterministic_hits=det_hits,
            llm_hits=llm_hits,
        )

    def redact_attrs(
        self,
        attrs: Mapping[str, Any],
        *,
        allowlist: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Redact a dict of OTel span attributes in place (returns a copy).

        Allowlisted keys pass through verbatim (counters, model names,
        well-known OTel fields). Everything else has its string value
        deterministically redacted. Non-string values are passed through.
        """
        if self._mode is RedactionMode.OFF:
            return dict(attrs)
        allow = set(allowlist or self._settings.redaction_allowlist_attrs)
        result: dict[str, Any] = {}
        for k, v in attrs.items():
            if k in allow:
                result[k] = v
                continue
            if isinstance(v, str):
                result[k] = self.redact(v).redacted
            elif isinstance(v, (int, float, bool)) or v is None:
                result[k] = v
            else:
                result[k] = self.redact(str(v)).redacted
        return result


_LLM_SYSTEM_PROMPT: Final[
    str
] = """\
You are a strict PII detector for German business documents.

Given a free-form German text snippet, identify any spans that contain
personally identifiable information ABOVE WHAT A REGEX WOULD CATCH. The
following are ALREADY HANDLED upstream and you should ignore them:

  - IBAN, BIC, USt-IdNr, Steuernummer, Personalausweis-Nr
  - email addresses, German phone numbers, SEPA mandate IDs

Detect:
  - personal names (Vornamen, Nachnamen, including Umlaute and ß)
  - private postal addresses (NOT company HQs, NOT generic city names)
  - other quasi-identifiers (Geburtsdatum + city, employee numbers)

Output ONLY a JSON array of {"span": "...", "category": "name|address|custom"}.
If no PII detected, output []. Do not output anything else.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Module-level convenience.
# ─────────────────────────────────────────────────────────────────────────────


_DEFAULT_ENGINE: RedactionEngine | None = None


def get_engine() -> RedactionEngine:
    """Process-singleton engine. Lazily constructed."""
    global _DEFAULT_ENGINE
    if _DEFAULT_ENGINE is None:
        _DEFAULT_ENGINE = RedactionEngine()
    return _DEFAULT_ENGINE


def install_engine(engine: RedactionEngine) -> None:
    """Replace the singleton. Tests + init() use this."""
    global _DEFAULT_ENGINE
    _DEFAULT_ENGINE = engine


def redact(text: str) -> str:
    """Convenience: deterministic redaction of a single string."""
    return get_engine().redact(text).redacted


__all__ = [
    "PIICategory",
    "RedactionEngine",
    "RedactionResult",
    "VaultProtocol",
    "get_engine",
    "install_engine",
    "redact",
]
