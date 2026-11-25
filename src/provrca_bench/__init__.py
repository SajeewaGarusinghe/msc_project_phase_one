"""FrameBench, ProvRCA and the comparator arms.

Implementation of *Verifying Telemetry Before Trusting It: Protecting LLM Root Cause Analysis
Agents from Fake Evidence in Microservices*. The knowledge base, including the build guide this
package follows and the G0 evidence it rests on, is the `Msc-project` repository.

What is here now (build-guide P1, items 1-7, and the A4 boundary):

    verdicts.py   A3's verdicts and reason codes, with the code -> verdict mapping as data
    contracts.py  A3's typed schemas: trace and log rows, sealed labels, verdicts, ledger, split
    defence.py    the single filter() interface every arm sees tool output through, and D0
    ledger.py     the run ledger and the study-level cost cap
    manifest.py   the split manifest and the test lock
    cases.py      the clean development cases with rebuilt derived files
    parity.py     BARO on released vs rebuilt derived files
    boundary.py   A4: sealed store, case layout, keyed variant names, launch_arm, leak scan

Still to come, with the phase that fills each:

    framebench/   P2  the overlay generator: (case, target, family, threat, dose, seed) -> bytes
    provrca/      P3  attribution, matcher, R1, R2, demotion
    arms/         P4  D1 authentication, D2 consistency, D3 source priority, D4 ProvRCA
    scoring/      P8  TBR, DiD, the wild cluster bootstrap-t, category rates
    trackb/       P5  Terraform and collector configs for the enforced-provenance track
"""
from provrca_bench.verdicts import DEMOTED, Reason, Verdict, is_demoted, verdict_for

__all__ = ["Reason", "Verdict", "verdict_for", "is_demoted", "DEMOTED"]
__version__ = "0.1.0"
