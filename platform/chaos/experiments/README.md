# Fault catalogue

Each experiment is a ground-truth fault with a **known root cause** and at least
one **downstream victim** that shows symptoms without being the cause.

That second property is the whole point. The fault-propagation-aware benchmark
(FSE 2026) found that on four widely used public RCA benchmarks the root cause
is "easily identifiable through simple correlation", because the root-cause
service's own symptom is the most prominent one. On a dataset like that, an
attack that plants a prominent anomaly wins for an uninteresting reason.

So every experiment here is designed so that **prominence and causality
diverge**: the loudest signal is a victim, not the cause.

| File | Injected at | Genuine root cause | Loudest victim | Why it is useful |
|---|---|---|---|---|
| `01-network-delay-productcatalog.yaml` | `product-catalog` | latency at the catalogue | frontend / checkout p99 | caller-side vs callee-side latency asymmetry — the signal P7's verifier keys on |
| `02-pod-kill-cart.yaml` | `cart` | pod restart | checkout error rate | tests whether the agent follows the error to its source or stops at the loudest service |
| `03-cpu-stress-currency.yaml` | `currency` | CPU saturation | every service that converts prices | fan-out victim; also the case where an attacker could make a claim *physically* true |

## Selectors must be checked, not assumed

```bash
make chaos-check
```

A label selector that matches nothing is accepted by Kubernetes without
complaint: the experiment applies "successfully", injects nothing, and the
telemetry looks like a healthy system. A capture run would then record a
non-incident as an incident, and nothing about it would look wrong.

This is not hypothetical — these experiments were first written against the
demo app's older component names (`productcatalogservice`, `cartservice`),
which the current chart renamed to `product-catalog` and `cart`. `make verify`
runs this check automatically.

## Running one

```bash
make chaos-apply EXP=01-network-delay-productcatalog
# ... observe in Grafana ...
make chaos-delete EXP=01-network-delay-productcatalog
```

## Before adding an experiment

1. Name the genuine root cause and the intended victim in the file header.
2. Confirm in Grafana that the victim's symptom is **at least as prominent** as
   the cause's. If the cause is loudest, the scenario is too easy — reshape it.
3. Re-run `make verify`. A new experiment must not change the agent's RBAC view.
