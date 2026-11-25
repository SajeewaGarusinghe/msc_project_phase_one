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

## What is here (build-guide P1, items 1–7, and the A4 boundary)

| Module | What it holds |
|---|---|
| `verdicts.py` | appendix A3's verdicts and reason codes, with the code → verdict mapping **as data** so a caller cannot record a decision A3 does not permit |
| `contracts.py` | A3's typed schemas: trace and log rows, sealed labels, verdict records, ledger rows, split entries |
| `defence.py` | the single `filter(tool, args, raw_output, sender)` interface every arm sees tool output through, `D0` as the identity function, and the append-only verdict log |
| `ledger.py` | the run ledger, written before a run starts, and the study-level cost cap |
| `manifest.py` | the split manifest with a SHA-256 per file, and the test lock (refuses S1–S3 before the `g3-freeze` tag) |
| `cases.py` | the 36 clean development cases with derived files rebuilt by `rcaeval-convert derive` |
| `parity.py` | BARO over released and rebuilt derived files (P1 item 5) |
| `boundary.py` | A4: the sealed store, the `cases/` layout, keyed variant names, `launch_arm`, and the A4-T2 leak scan |
| `framebench/` | P2 (started 25 Nov): target rule, doses, the variant builder and the dev-set generator — see below |

Two constraints from A3 are enforced by the schemas rather than left to care: a service name may
contain no `_` (RCAEval reads the service as the text before the first underscore), and a row
carries the fault-window check that `tracets_lat`'s pre-fault baseline makes necessary.

## Still to come

| Module | Phase | What fills it |
|---|---|---|
| `provrca/` | P3 | attribution → one-to-one matcher → applicability → R1/R2, or *unknown* with a reason |
| `arms/` | P4 | D1 authentication, D2 consistency (from the published specifications — no code was released), D3 source priority, D4 ProvRCA |
| `scoring/` | P8 | TBR, the difference-in-differences, the wild cluster bootstrap-t with Webb weights, category rates |
| `trackb/` | P5 | the enforced-provenance deployment; `g0_pilot/stage2/trackb/` is the working spike to build from |

## Running the tests

```bash
uv sync --extra dev
scripts/a4_users.sh               # once per machine: the sealer and arm users A4-T1 needs (sudo)
uv run pytest -q
uv run pytest -q -k d0_identity   # P1-T4
uv run pytest -q -k a4            # A4-T1, A4-T2
```

**A4-T1** builds the real layout and starts a probe *as the arm user* through `launch_arm`: it
must be denied the sealed file, the sealed directory, its own case directory (sibling variants)
and `cases/` (other cases), and must still read its own variant (the control). Without sudo or
the two users it is skipped on a workstation, and **fails** under CI or with
`PROVRCA_REQUIRE_A4=1`, so CI cannot go green without the boundary.

**A4-T2** replays every tool call of the G0 pilot's 144 runs through `CaseToolkit`, so it scans
the exact outputs the agent saw (each replay's size is checked against the trajectory's
`result_chars`), plus every prompt and trajectory line. The pilot's case data is 14 GB and lives
in `g0_pilot`, so the scan is run by hand and its record is committed:

```bash
PYTHONPATH=src ~/g0_pilot/.venv/bin/python -m provrca_bench.boundary \
  --labels ~/g0_pilot/pilot_sealed/labels.jsonl --cases ~/g0_pilot/pilot_tasks/pilot/cases \
  --trajectories ~/g0_pilot/pilot_preds.jsonl.trajectories --out evidence/a4_t2_pilot.json
```

(The pilot's venv is used because `CaseToolkit` imports scikit-learn, which this package does
not depend on.) CI tests the scanner on planted leaks and checks the committed record.

**P1-T3** intervals: `scripts/parity_intervals.py` → `evidence/p1_t3_parity.json`.

## FrameBench (P2, in progress)

```bash
PROVRCA_HARNESS_PYTHON=~/g0_pilot/.venv/bin/python uv run python -m provrca_bench.framebench.generate \
  --dataset parity/dataset-rebuilt --tasks parity/tasks-rebuilt/re2-ob/cases \
  --answers parity/tasks-rebuilt/re2-ob.answers.jsonl \
  --out cases/framebench-dev --sealed sealed/framebench-dev --seed 20261125 --workers 6
PROVRCA_HARNESS_PYTHON=~/g0_pilot/.venv/bin/python uv run pytest -q tests/test_framebench.py
```

`derive` needs RCAEval's `convert` extra (drain3), which this package does not depend on, so
`PROVRCA_HARNESS_PYTHON` names an interpreter that has it, as P1 item 4 did.

- **Target rule:** the four checks of proposal section 12, with check (a) read as "not BARO's top
  service" (K21). It reproduces G0 step 8 exactly: 0 score mismatches over 396 candidate rows,
  3.39 eligible targets per case, and 51 callee / 71 sibling.
- **Doses** were decided by the owner on 25 Nov after a data check (Msc-project decisions log):
  - **F2/FS:** a pooled development reference, share 0.4975 at code 13 (low dose is half). 34 of
    36 cases have no genuine error-rate rise to match.
  - **F1/F4:** 20 lines high, 10 low, because the root cause writes no error lines in any case.
  - **F3:** the factor that brings the target's p95 rise up to the case's largest genuine one.
- **Built so far:** T2 variants of F1, F2, F3, FS, F4 and A1. T1 and the adaptive families need
  the P3 matcher, so P2-T6 is an expected failure.

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

## CI is not installed yet (a token-scope limit, not a decision)

The workflow is written and lives at **`ci/github-actions-ci.yml`**, not at
`.github/workflows/ci.yml`, because the token this repository is pushed with lacks GitHub's
`workflow` scope: a push that touches `.github/workflows/` is rejected outright with
*"refusing to allow a Personal Access Token to create or update workflow … without `workflow`
scope"*. Until that scope is granted, CI does not run on push.

To install it, either grant the token the `workflow` scope and `git mv ci/github-actions-ci.yml
.github/workflows/ci.yml`, or add the file through the GitHub web UI. The build guide requires
"CI runs unit tests and the A4 tests on every push" (P1 item 1), so **this is an open P1 item**,
not a finished one.
