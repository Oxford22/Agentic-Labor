"""Alert classification + signature verification."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from putsch_obs.alerts import Severity, classify, handle_event


@pytest.mark.parametrize(
    ("event", "value", "expected"),
    [
        ("redaction_failed", None, Severity.CRITICAL),
        ("exception_rate", 0.30, Severity.CRITICAL),
        ("exception_rate", 0.05, Severity.INFO),
        ("cost_spike_ratio", 6.0, Severity.CRITICAL),
        ("latency_p95_slo_breach", 2.5, Severity.HIGH),
        ("quality_score_drop_24h", -0.25, Severity.HIGH),
        ("quality_score_drop_24h", -0.05, Severity.INFO),
        ("dropped_spans_per_hour", 200, Severity.WARNING),
        ("dataset_version_churn", 6, Severity.WARNING),
        ("anything_else", None, Severity.INFO),
    ],
)
def test_classify(event: str, value: float | None, expected: Severity) -> None:
    payload = {"event": event, "value": value} if value is not None else {"event": event}
    assert classify(event, payload) is expected


async def test_signature_invalid_returns_none(isolated_env: None) -> None:
    body = json.dumps({"event": "redaction_failed"}).encode()
    res = await handle_event(
        raw_body=body,
        signature_hex="00" * 32,
        secret="shhh",
    )
    assert res is None


async def test_signature_valid_emits_alert(isolated_env: None) -> None:
    body = json.dumps({"event": "redaction_failed", "value": None}).encode()
    sig = hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
    alert = await handle_event(raw_body=body, signature_hex=sig, secret="shhh")
    assert alert is not None
    assert alert.severity is Severity.CRITICAL
    assert alert.source_event == "redaction_failed"
