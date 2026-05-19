"""Audit chain hashing — verifying the WORM log is tamper-evident."""

from __future__ import annotations

import hashlib
import json

from putsch_obs.vault.audit import _canonical_json, verify_chain  # type: ignore[attr-defined]


def _row(payload: dict[str, object], prev: str) -> dict[str, object]:
    pb = _canonical_json(payload)
    row_hash = hashlib.sha256(prev.encode("ascii") + pb).hexdigest()
    return {"payload": payload, "row_hash": row_hash, "prev_hash": prev}


def test_chain_verify_accepts_valid_chain() -> None:
    rows: list[dict[str, object]] = []
    prev = "0" * 64
    for i in range(4):
        payload = {"actor": "ops", "reason": f"r{i}", "i": i}
        row = _row(payload, prev)
        rows.append(row)
        prev = str(row["row_hash"])
    assert verify_chain(rows)


def test_chain_verify_detects_tamper() -> None:
    rows: list[dict[str, object]] = []
    prev = "0" * 64
    for i in range(3):
        payload = {"actor": "ops", "reason": f"r{i}"}
        row = _row(payload, prev)
        rows.append(row)
        prev = str(row["row_hash"])
    # Tamper with the middle row's payload, leaving its hash intact.
    tampered = dict(rows[1])
    tampered["payload"] = {"actor": "attacker", "reason": "r1"}
    rows[1] = tampered
    assert not verify_chain(rows)


def test_chain_verify_canonical_json_is_stable() -> None:
    a = _canonical_json({"b": 1, "a": 2})
    b = _canonical_json({"a": 2, "b": 1})
    assert a == b
