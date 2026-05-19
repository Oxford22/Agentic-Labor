"""Langfuse webhook → Teams alerts.

Langfuse posts alert events to ``POST /webhooks/langfuse`` (handled by
whatever app embeds this SDK). The webhook hands the payload to
:func:`handle_event`, which:

1. Validates the Langfuse signature (HMAC-SHA256 using the shared secret).
2. Classifies severity via the rubric below.
3. Builds an Adaptive Card and POSTs it to Microsoft Graph.

Severity rubric
---------------
| Severity   | Trigger                                                              |
| ---------- | -------------------------------------------------------------------- |
| critical   | redaction failure; exception-rate > 25%; cost-spike > 5× 24h median  |
| high       | latency p95 > 2× SLO; quality_score drop > 0.2 over 24h              |
| warning    | dataset version churn > 5/week; dropped-span counter > 100/hour      |
| info       | everything else (eval run complete, dashboard refresh, etc.)         |

The mapping lives in :data:`SEVERITY_TRIGGERS`. Operators edit it; the
runbook explains the workflow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from putsch_obs.config import PutschObsSettings, get_settings
from putsch_obs.logging import get_logger

log = get_logger(__name__)


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    WARNING = "warning"
    INFO = "info"


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


@dataclass(slots=True, frozen=True)
class Alert:
    severity: Severity
    title: str
    body: str
    source_event: str
    payload: Mapping[str, Any]


SEVERITY_TRIGGERS: tuple[tuple[Severity, str, Any], ...] = (
    # (severity, event_name, threshold)
    (Severity.CRITICAL, "redaction_failed", None),
    (Severity.CRITICAL, "exception_rate", 0.25),
    (Severity.CRITICAL, "cost_spike_ratio", 5.0),
    (Severity.HIGH, "latency_p95_slo_breach", 2.0),
    (Severity.HIGH, "quality_score_drop_24h", -0.20),
    (Severity.WARNING, "dataset_version_churn", 5),
    (Severity.WARNING, "dropped_spans_per_hour", 100),
)


def classify(event_name: str, payload: Mapping[str, Any]) -> Severity:
    """Apply the severity rubric. Falls back to INFO."""
    for sev, name, threshold in SEVERITY_TRIGGERS:
        if name != event_name:
            continue
        if threshold is None:
            return sev
        try:
            value = float(payload.get("value", 0.0))
        except (TypeError, ValueError):
            continue
        if name == "quality_score_drop_24h":
            # Threshold is negative; matches if value <= threshold.
            if value <= float(threshold):
                return sev
        else:
            if value >= float(threshold):
                return sev
    return Severity.INFO


def _verify(secret: str, raw_body: bytes, signature_hex: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_hex)


async def handle_event(
    *,
    raw_body: bytes,
    signature_hex: str,
    secret: str,
    settings: PutschObsSettings | None = None,
    teams_client: httpx.AsyncClient | None = None,
) -> Alert | None:
    """Process a Langfuse webhook event. Returns the emitted Alert or None."""
    cfg = settings or get_settings()
    if not _verify(secret, raw_body, signature_hex):
        log.warning("alerts.signature_invalid")
        return None
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        log.warning("alerts.body_invalid", err=str(exc))
        return None
    event_name = str(payload.get("event") or payload.get("type") or "unknown")
    severity = classify(event_name, payload)
    min_sev = Severity(cfg.alerts_min_severity)
    if _SEVERITY_ORDER[severity] < _SEVERITY_ORDER[min_sev]:
        log.info("alerts.below_threshold", event=event_name, severity=severity.value)
        return None

    title = _title(event_name, severity)
    body = _body(payload)
    alert = Alert(
        severity=severity,
        title=title,
        body=body,
        source_event=event_name,
        payload=payload,
    )
    if not cfg.alerts_enabled or cfg.teams_webhook_url is None:
        log.info("alerts.suppressed", reason="alerts_disabled_or_no_webhook")
        return alert
    await _post_to_teams(alert, cfg, teams_client)
    return alert


async def _post_to_teams(
    alert: Alert,
    cfg: PutschObsSettings,
    client: httpx.AsyncClient | None,
) -> None:
    webhook = (
        cfg.teams_webhook_url.get_secret_value()
        if cfg.teams_webhook_url is not None
        else None
    )
    if not webhook:
        return
    color = {
        Severity.CRITICAL: "attention",
        Severity.HIGH: "warning",
        Severity.WARNING: "accent",
        Severity.INFO: "good",
    }[alert.severity]
    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "size": "Large",
                            "weight": "Bolder",
                            "color": color,
                            "text": f"[{alert.severity.value.upper()}] {alert.title}",
                        },
                        {"type": "TextBlock", "wrap": True, "text": alert.body},
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Source event", "value": alert.source_event},
                                {"title": "Service", "value": cfg.service_name},
                                {"title": "Env", "value": cfg.deployment_environment},
                            ],
                        },
                    ],
                },
            }
        ],
    }
    own = client or httpx.AsyncClient(timeout=5.0)
    try:
        try:
            resp = await own.post(webhook, json=card)
            if resp.status_code >= 400:
                log.warning(
                    "alerts.post_failed",
                    status=resp.status_code,
                    body=resp.text[:400],
                )
        finally:
            if client is None:
                await own.aclose()
    except httpx.HTTPError as exc:
        log.warning("alerts.post_exception", err=str(exc))


def _title(event_name: str, severity: Severity) -> str:
    nice = event_name.replace("_", " ").title()
    return f"{severity.value.upper()}: {nice}"


def _body(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for k in ("value", "threshold", "service", "trace_id", "url"):
        if k in payload and payload[k] is not None:
            parts.append(f"**{k}**: `{payload[k]}`")
    note = payload.get("message") or payload.get("description")
    if note:
        parts.append(str(note))
    return "\n\n".join(parts) or "(no body)"


__all__ = ["Alert", "Severity", "SEVERITY_TRIGGERS", "classify", "handle_event"]
