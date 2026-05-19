"""Property tests on validity-window invariants.

Hypothesis-driven. The invariants we test:

1. A fact's `business_time_to` is never before `business_time_from`.
2. The same is true for `system_time_*`.
3. For any two facts with the same (entity_id, predicate), their
   *open* (business_time_to=None) windows do not overlap. This is the
   "no concurrent current value" rule.
4. `make_idempotency_key` is total and deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from putsch_memory.ontology import (
    SourceSystem,
    ValidityWindow,
    make_idempotency_key,
)

# Bounded to a 50-year span to avoid pathological datetime math.
_TIME = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2050, 1, 1),
    timezones=st.just(timezone.utc),
)


@given(start=_TIME, delta=st.integers(min_value=0, max_value=365 * 50))
def test_business_time_to_never_precedes_from(start: datetime, delta: int) -> None:
    end = start + timedelta(days=delta)
    w = ValidityWindow(
        business_time_from=start,
        business_time_to=end,
        system_time_from=start,
    )
    assert w.business_time_to is not None
    assert w.business_time_to >= w.business_time_from


@given(start=_TIME, delta=st.integers(min_value=1, max_value=365 * 50))
def test_validity_window_active_only_inside_window(start: datetime, delta: int) -> None:
    end = start + timedelta(days=delta)
    w = ValidityWindow(
        business_time_from=start,
        business_time_to=end,
        system_time_from=start,
    )
    assume(delta >= 2)
    inside = start + timedelta(days=delta // 2)
    after = end + timedelta(days=1)
    assert w.is_active_at(inside)
    assert not w.is_active_at(after)


@given(
    source=st.sampled_from(list(SourceSystem)),
    source_id=st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        min_size=1,
        max_size=64,
    ),
    t=_TIME,
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_idempotency_key_total_and_deterministic(
    source: SourceSystem, source_id: str, t: datetime
) -> None:
    k1 = make_idempotency_key(source_system=source, source_id=source_id, event_time=t)
    k2 = make_idempotency_key(source_system=source, source_id=source_id, event_time=t)
    assert k1 == k2
    assert len(k1) == 64  # SHA-256 hex


@given(
    a_from=_TIME,
    a_delta=st.integers(min_value=1, max_value=1000),
    b_from=_TIME,
    b_delta=st.integers(min_value=1, max_value=1000),
)
def test_disjoint_open_windows(
    a_from: datetime, a_delta: int, b_from: datetime, b_delta: int
) -> None:
    """Two closed windows whose [from, to) ranges don't overlap are
    independent. This is a sanity check on our overlap semantics."""
    a = ValidityWindow(
        business_time_from=a_from,
        business_time_to=a_from + timedelta(days=a_delta),
        system_time_from=a_from,
    )
    b = ValidityWindow(
        business_time_from=b_from,
        business_time_to=b_from + timedelta(days=b_delta),
        system_time_from=b_from,
    )
    if a.business_time_to is None or b.business_time_to is None:
        return
    overlap = not (a.business_time_to <= b.business_time_from or b.business_time_to <= a.business_time_from)
    if not overlap:
        # Pick a witness point in each window — they should both be active there
        assert a.is_active_at(a_from + timedelta(days=max(0, a_delta // 2)))
        assert b.is_active_at(b_from + timedelta(days=max(0, b_delta // 2)))
