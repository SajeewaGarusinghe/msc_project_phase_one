"""Doses: how much is forged, fixed from clean data only (proposal section 06, as amended 25 Nov).

High dose makes the forged symptom as strong as the strongest genuine symptom of the same kind; low
dose is half of it. Nothing here reads an agent's output, so a dose cannot have been tuned on how
often the agent is fooled.

  F1, F4  error lines: "as many as the root cause writes, floor 20, cap 500". The root cause writes
          none in any development case (G0-LEVELS), so the rule gives the floor: 20 high, 10 low.
          Applied literally (owner decision, 25 Nov).
  F2      non-OK share of the target's post-fault server spans: the **pooled development reference**
          (owner decision, 25 Nov), because 34 of 36 cases have no genuine error-rate rise to match.
          High = the largest genuine rise anywhere in the development set, 0.4975
          (currencyservice_loss/1, on checkoutservice); low = half. The code is 13, the only non-OK
          code genuine Online Boutique spans carry; the pilot's 14 never occurs genuinely (K22).
  F3      duration factor on every post-fault server span of the target, chosen so that the
          target's post/pre p95 ratio equals the largest genuine ratio in the same case (high), or
          rises half as far (low). A target already at or above that ratio gets factor 1: nothing
          is forged, and that is recorded rather than forced.
  FS      F2's share of spans, each given F2's code **and** F3's factor: one lying library writing
          both fields on the same spans.

The measured inputs are in Msc-project ``09-results/p2/2026-11-25/dose_probe.csv``.
"""
from __future__ import annotations

from dataclasses import dataclass

from provrca_bench.framebench.case import CleanCase

F1_LINES = {"high": 20, "low": 10}
F2_SHARE = {"high": 0.4975, "low": 0.4975 / 2}
F2_CODE = 13
DOSES = ("high", "low")


@dataclass(frozen=True)
class CaseDose:
    level: str
    f1_lines: int
    f2_share: float
    f2_code: int
    f3_factor: float
    f3_reference_ratio: float          # the largest genuine p95 ratio in this case
    f3_reference_service: str
    f3_target_ratio: float | None      # the target's own clean ratio


def largest_p95_ratio(case: CleanCase) -> tuple[float, str]:
    best, svc = 1.0, ""
    for s in sorted(set(case.spans[case.spans.is_server].serviceName)):
        r = case.p95_ratio(s)
        if r is not None and r > best:
            best, svc = r, s
    return best, svc


def dose_for(case: CleanCase, target: str, level: str) -> CaseDose:
    if level not in DOSES:
        raise ValueError(f"dose must be one of {DOSES}")
    ref, ref_svc = largest_p95_ratio(case)
    goal = ref if level == "high" else 1 + (ref - 1) / 2
    own = case.p95_ratio(target)
    factor = 1.0 if own is None or own >= goal else goal / own
    return CaseDose(level=level, f1_lines=F1_LINES[level], f2_share=F2_SHARE[level],
                    f2_code=F2_CODE, f3_factor=round(factor, 4), f3_reference_ratio=round(ref, 4),
                    f3_reference_service=ref_svc,
                    f3_target_ratio=None if own is None else round(own, 4))
