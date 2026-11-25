"""Attribution, the first link of the decision chain (build guide P3 item 1; also arm D1).

A record's *claimed* writer is the service name it carries; its *sender* is the identity the
collector attached when the record arrived (FrameBench writes it as a ``sender`` column on every
record, clean and injected, test P2-T5). A record whose claim differs from its sender is
``ATTR_MISMATCH`` and rejected.

This module is the **only** reader of ``sender`` in ProvRCA. The matcher must not read it: if it
did, it would perform attribution a second time, and an arm with attribution switched off would
still be credited with attribution's effect (proposal §05, "each layer gets the credit it earns";
build-guide P2-T6).

Names are compared within one name space. Trace records claim a trace service name
(``frontendservice``) and their sender is one too; log records claim a container name
(``frontend``) and their sender is a container name. ``sender_map`` translates a sender identity
into the claim it entitles; on Track A it is the identity, on Track B it is a configured
pod-to-service table (P5), never a string heuristic.
"""
from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

SENDER = "sender"


class MissingSender(ValueError):
    """The case carries no sender attribute. A configuration error, not a verdict: A3 has no
    reason code for it, and FrameBench guarantees the column (P2-T5). Raised when the case is
    loaded for an arm that runs attribution, never part-way through a run."""


def require_sender(frame: pd.DataFrame, what: str) -> None:
    if SENDER not in frame.columns:
        raise MissingSender(f"{what} has no '{SENDER}' column; attribution cannot run")
    if frame[SENDER].isna().any() or (frame[SENDER].astype(str).str.strip() == "").any():
        raise MissingSender(f"{what} has records with an empty '{SENDER}'")


def _mismatch(claimed: pd.Series, sender: pd.Series,
              sender_map: Mapping[str, str] | None) -> pd.Series:
    entitled = sender.astype(str)
    if sender_map:
        entitled = entitled.map(lambda s: sender_map.get(s, s))
    return claimed.astype(str) != entitled


def rejected_spans(spans: pd.DataFrame, sender_map: Mapping[str, str] | None = None) -> pd.Series:
    """Boolean mask: True where a span's ``serviceName`` is not what its sender may claim."""
    require_sender(spans, "traces")
    return _mismatch(spans["serviceName"], spans[SENDER], sender_map)


def rejected_logs(logs: pd.DataFrame, sender_map: Mapping[str, str] | None = None) -> pd.Series:
    """Boolean mask: True where a log line's ``container_name`` is not what its sender may claim."""
    require_sender(logs, "logs")
    return _mismatch(logs["container_name"], logs[SENDER], sender_map)


#: The outcome of a log line that passed attribution. No rule links a log line to a request
#: outcome (proposal §06, F1: "Unknown"), so it has no witness; decided 25 Nov (decisions log).
LOG_DETAIL = "no_log_rule"
