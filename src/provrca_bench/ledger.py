"""The run ledger and the cost cap (build-guide P1 item 7).

Every run gets a row *before* it starts and the row is updated when it ends, so a run that dies
leaves a `running` row rather than no row at all -- the difference between a crash and a run that
was never attempted is exactly what a budget audit needs.

The ledger is a CSV because it is read by hand as often as by code. It is rewritten in full on
each update, which is safe at this scale (about 12,784 runs) and avoids a partial-line write.
"""
from __future__ import annotations

import csv
from pathlib import Path

from provrca_bench.contracts import LedgerRow

FIELDS = list(LedgerRow.model_fields)


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def rows(self) -> list[LedgerRow]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as f:
            out = []
            for raw in csv.DictReader(f):
                out.append(LedgerRow(
                    run_id=raw["run_id"], cell=raw["cell"], case_id=raw["case_id"],
                    arm=raw["arm"], seed=int(raw["seed"]), model=raw["model"],
                    tokens_in=int(raw["tokens_in"] or 0), tokens_out=int(raw["tokens_out"] or 0),
                    cost_usd=float(raw["cost_usd"] or 0), status=raw["status"]))
            return out

    def _write(self, rows: list[LedgerRow]) -> None:
        with self.path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for row in rows:
                w.writerow(row.model_dump(mode="json"))

    def plan(self, row: LedgerRow) -> LedgerRow:
        """Record a run before it starts. Re-planning an existing run id is refused."""
        rows = self.rows()
        if any(r.run_id == row.run_id for r in rows):
            raise ValueError(f"run {row.run_id} is already in the ledger")
        rows.append(row)
        self._write(rows)
        return row

    def update(self, run_id: str, **changes) -> LedgerRow:
        rows = self.rows()
        for i, row in enumerate(rows):
            if row.run_id == run_id:
                rows[i] = row.model_copy(update=changes)
                self._write(rows)
                return rows[i]
        raise KeyError(f"run {run_id} is not in the ledger; plan it before updating it")

    def spent_usd(self) -> float:
        return sum(r.cost_usd for r in self.rows())

    def check_cap(self, cap_usd: float) -> None:
        """Raise before a run starts if the budget is already spent. Paired with RCAEval's
        ``--max-cost-usd``, which caps a single run; this caps the study."""
        spent = self.spent_usd()
        if spent >= cap_usd:
            raise RuntimeError(f"cost cap reached: ${spent:.2f} of ${cap_usd:.2f} spent")
