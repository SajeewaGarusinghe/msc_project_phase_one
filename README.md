# provrca-bench

Implementation of **FrameBench**, **ProvRCA** and the comparator arms for the MSc project
*Verifying Telemetry Before Trusting It: Protecting LLM Root Cause Analysis Agents from Fake
Evidence in Microservices*.

The knowledge base — the proposal, the build guide this package follows, and the G0 evidence it
rests on — is the [`Msc-project`](https://github.com/SajeewaGarusinghe/Msc-project) repository.
The G0 pilot and the G0 stage 2 analysis scripts are in
[`g0_pilot`](https://github.com/SajeewaGarusinghe/g0_pilot).

**G0 passed on 14 November 2026**, so the build order's gate is open and work on the rules may
start. The decision it produced scopes this package: anything that depends on R1 or R2 is measured
on the Online Boutique strata, and H2b is reported as estimation with intervals rather than as a
confirmatory test (`Msc-project/09-results/g0/G0_report.md`).

## What is here (build-guide P1, items 1, 2, 6, 7)

| Module | What it holds |
|---|---|
| `verdicts.py` | appendix A3's verdicts and reason codes, with the code → verdict mapping **as data** so a caller cannot record a decision A3 does not permit |
| `contracts.py` | A3's typed schemas: trace and log rows, sealed labels, verdict records, ledger rows, split entries |
| `defence.py` | the single `filter(tool, args, raw_output, sender)` interface every arm sees tool output through, `D0` as the identity function, and the append-only verdict log |
| `ledger.py` | the run ledger, written before a run starts, and the study-level cost cap |

Two constraints from A3 are enforced by the schemas rather than left to care: a service name may
contain no `_` (RCAEval reads the service as the text before the first underscore), and a row
carries the fault-window check that `tracets_lat`'s pre-fault baseline makes necessary.

## Still to come

| Module | Phase | What fills it |
|---|---|---|
| `framebench/` | P2 | the overlay generator: `(clean case, target, family, threat model, dose, seed) → bytes + sealed labels`, deterministic |
| `provrca/` | P3 | attribution → one-to-one matcher → applicability → R1/R2, or *unknown* with a reason |
| `arms/` | P4 | D1 authentication, D2 consistency (from the published specifications — no code was released), D3 source priority, D4 ProvRCA |
| `scoring/` | P8 | TBR, the difference-in-differences, the wild cluster bootstrap-t with Webb weights, category rates |
| `trackb/` | P5 | the enforced-provenance deployment; `g0_pilot/stage2/trackb/` is the working spike to build from |

## Running the tests

```bash
uv sync --extra dev
uv run pytest -q
uv run pytest -q -k d0_identity   # P1-T4
uv run pytest -q -k a4            # A4-T1, A4-T2
```

40 pass; **2 are skipped on purpose**. A4-T1 (an arm cannot open a sealed file or list sibling
variants) and A4-T2 (no trajectory names the target beside "fake", "injected" or a family code)
need the runner and the separate Unix user from P1 item 3 onward. They are declared and skipped
with the reason, rather than written to pass vacuously, so the gap is visible in CI output.

## On pinning RCAEval

The build guide asks for RCAEval to be pinned **by commit**. The published distribution cannot
support that: `rcaeval-1.12.0.dist-info/METADATA` declares neither a `Home-page` nor a
`Project-URL`, so there is no repository to pin to. `rcaeval==1.12.0` plus the hashes in `uv.lock`
is the strongest pin available from PyPI, and the project's archived copy of the package
(proposal §07) is what a commit pin would otherwise give. Noted here rather than silently
substituted.

## What this package must never do

The information boundary (A4) is not advice. No arm may read the target identity, the family, the
dose, the seed, the three per-record labels, or any other case during a run. Sealed labels live
outside the case tree and only the scorer reads them. The schemas refuse a case row that carries a
label field, which is the most plausible way for one to leak.
