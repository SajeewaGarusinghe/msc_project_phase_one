"""The defence-layer interface (build-guide P1 item 6).

Every arm sees tool output through exactly one function, so the arms differ only in what that
function does and never in what they are allowed to read:

    filter(tool, args, raw_output, sender) -> (output, verdicts)

**D0 is the identity function.** Test P1-T4 requires its output to be byte-identical to the raw
CaseToolkit output, which is why `D0.filter` returns the same object it was given rather than a
copy, a reserialisation or a normalised form -- any of those would pass an equality check while
failing a byte comparison on some input.

Every verdict is recorded, including the compatible and unknown ones, so P8 can audit clean
removals: ProvRCA writes the full per-item verdict table once per (run, case) as
`verdicts-<case>.csv.gz`, and one `audit.jsonl` line per withheld item with its rule inputs
(`provrca/demotion.py`; an amendment to A3's "one line per evidence item", decisions log 25 Nov).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from provrca_bench.contracts import VerdictRecord
from provrca_bench.verdicts import Reason, is_demoted


class DefenceLayer(ABC):
    """One wrapper around CaseToolkit and the MCP server. Arms are distinguished only by this."""

    #: the arm identifier used in the ledger
    arm: str

    @abstractmethod
    def filter(self, tool: str, args: dict[str, Any], raw_output: Any,
               sender: str | None) -> tuple[Any, list[VerdictRecord]]:
        """Return what the agent may see, and every verdict formed on the way."""


class D0(DefenceLayer):
    """No defence: the undefended agent. The identity function, by construction."""

    arm = "D0"

    def filter(self, tool: str, args: dict[str, Any], raw_output: Any,
               sender: str | None) -> tuple[Any, list[VerdictRecord]]:
        return raw_output, []


class AuditLog:
    """Append-only verdict log (`runs/<run>/audit.jsonl`, A3).

    Opened in append mode and flushed per line: a run that dies mid-way must leave the verdicts it
    had already formed, because a truncated audit log cannot be distinguished from a run that
    formed no verdicts.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, records: list[VerdictRecord]) -> int:
        if not records:
            return 0
        with self.path.open("a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n")
                f.flush()
        return len(records)

    def read(self) -> list[VerdictRecord]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [VerdictRecord.model_validate_json(line) for line in f if line.strip()]

    def withheld(self, demoted: frozenset[str] | None = None) -> list[VerdictRecord]:
        """The records a demoting arm removed from tool output. ``demoted`` is the arm's set of
        verdicts (D1: rejected; D4: rejected and contradicted); the default is A3's full set."""
        if demoted is None:
            return [r for r in self.read() if is_demoted(Reason(r.reason))]
        return [r for r in self.read() if r.verdict.value in demoted]
