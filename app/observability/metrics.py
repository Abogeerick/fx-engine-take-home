"""Prometheus metrics per SPEC §6.

Module-level registry with five counters/histograms. Routes and
services import and call ``.inc()`` / ``.observe()`` directly; no
indirection layer.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

QUOTE_TOTAL = Counter(
    "fx_quote_total",
    "Total quote requests, labelled by routing.",
    labelnames=("routing",),
)

EXECUTE_TOTAL = Counter(
    "fx_execute_total",
    "Total execute requests, labelled by status.",
    labelnames=("status",),
)

RATE_FETCH_LATENCY = Histogram(
    "fx_rate_fetch_latency_seconds",
    "Latency of rate-source fetches (successful or failed).",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

RATE_FETCH_FAILURE_TOTAL = Counter(
    "fx_rate_fetch_failure_total",
    "Total rate-source fetch failures.",
)

IDEMPOTENT_REPLAY_TOTAL = Counter(
    "fx_idempotent_replay_total",
    "Total idempotent execute replays (HTTP 200, body returned from store).",
)
