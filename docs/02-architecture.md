# Architecture

Phase 1 delivers the **bench**: a reproducible cluster that produces honest
telemetry from known faults, with the fault injector hidden from the agent under
test. Later phases (capture, forgery, defence) build on it without changing it.

## Layers

```mermaid
flowchart TB
    subgraph INFRA["infra/ — one node, two ways to get it"]
        TF["Terraform<br/>EC2 + k3s<br/><i>TARGET=cloud</i>"]
        K3D["k3d config<br/>k3s in Docker<br/><i>TARGET=local</i>"]
    end

    subgraph PLATFORM["platform/ — identical on both targets"]
        OBS["observability/<br/>Collector · Prometheus · Tempo · Loki"]
        APP["app/<br/>Astronomy Shop"]
        CHAOS["chaos/<br/>Chaos Mesh + fault catalogue"]
        RBAC["rbac/<br/>agent identity + trust boundary"]
    end

    subgraph NEXT["later phases — not in this repo yet"]
        REC["P2 recorder<br/>+ mock tool server"]
        VER["P7 provenance verifier"]
    end

    TF  --> PLATFORM
    K3D --> PLATFORM
    APP -->|OTLP| OBS
    CHAOS -.->|injects faults into| APP
    OBS --> REC
    RBAC --> REC
    OBS -.->|independent signals| VER

    style CHAOS fill:#fdecec,stroke:#c0392b
    style RBAC  fill:#eaf7f1,stroke:#1b9e77
    style NEXT  fill:#fafafa,stroke:#bbb,stroke-dasharray: 4 3
```

## Runtime topology

Everything lives on one node. Namespaces carry the trust boundary.

```mermaid
flowchart LR
    subgraph NODE["single k3s node"]
        subgraph APPNS["ns: app — agent-visible"]
            SVCS["~15 Astronomy Shop services"]
            LOAD["Locust load generator"]
        end

        subgraph OBSNS["ns: observability — agent-visible"]
            COL["OTel Collector<br/>gateway"]
            PROM["Prometheus"]
            TEMPO["Tempo"]
            LOKI["Loki"]
            GRAF["Grafana<br/><i>humans only</i>"]
        end

        subgraph CHAOSNS["ns: chaos-mesh — NEVER agent-visible"]
            CM["controller + CRDs"]
            CD["chaos-daemon<br/>privileged"]
        end

        subgraph RESNS["ns: research — harness"]
            SA["ServiceAccount<br/>rca-agent"]
        end
    end

    LOAD --> SVCS
    SVCS -->|"OTLP 4318<br/>traces · metrics · logs"| COL
    COL -->|remote-write| PROM
    COL -->|OTLP| TEMPO
    COL -->|OTLP| LOKI
    PROM --> GRAF
    TEMPO --> GRAF
    LOKI --> GRAF
    CD -.->|"injects faults"| SVCS
    SA -->|"read-only"| APPNS
    SA -->|"read-only"| OBSNS
    SA -.->|"NO ACCESS"| CHAOSNS

    style CHAOSNS fill:#fdecec,stroke:#c0392b,stroke-width:2px
    style RESNS fill:#eef4fb,stroke:#2d72b8
    style GRAF fill:#fff4e0,stroke:#ec7211
```

## Telemetry path, and why the Collector is a single gateway

```mermaid
sequenceDiagram
    participant S as service<br/>(app ns)
    participant C as OTel Collector
    participant K as k8sattributes
    participant B as Prometheus / Tempo / Loki

    S->>C: OTLP — self-reported resource attributes
    Note over C,K: k8sattributes resolves the sender's pod IP<br/>to real cluster metadata
    C->>K: enrich
    K-->>C: k8s.pod.name, k8s.namespace.name,<br/>k8s.deployment.name — from the CLUSTER
    C->>B: export with cluster-derived identity
```

Everything funnels through one Collector on purpose. It is:

1. the **single place to attribute telemetry** — `k8sattributes` overwrites what a
   workload claims about itself with what the cluster knows, which is a weak form
   of the authorship check Phase 7 generalises;
2. the **natural insertion point** for the Phase 7 provenance verifier, which
   becomes a processor in this pipeline rather than a change to every service;
3. the **single tap** the Phase 2 recorder reads from.

## Fault injection and the two signal classes

```mermaid
flowchart LR
    CHAOS["Chaos Mesh<br/>ground truth"] -->|real fault| CAUSE["root-cause service"]
    CAUSE -->|propagates| VICTIM["victim services<br/><i>loudest symptom</i>"]

    CAUSE  -->|self-reported| APPTEL["application telemetry<br/>logs · metrics · traces"]
    VICTIM -->|self-reported| APPTEL

    CAUSE  -.->|kernel-authored| INDEP["independent signals<br/>cgroup counters · caller spans"]
    VICTIM -.->|kernel-authored| INDEP

    APPTEL --> AGENT["RCA agent"]
    INDEP  -.->|"Phase 7 only"| VERIF["provenance verifier"]

    style INDEP fill:#eaf7f1,stroke:#1b9e77
    style CHAOS fill:#fdecec,stroke:#c0392b
```

The distinction set up here is what the whole project turns on:

- **Application telemetry is self-reported.** A service says how it is doing. In
  every backend, "which service emitted this" is just a label set by the sender,
  authenticated by nothing.
- **Independent signals are authored elsewhere** — the caller's own span, the
  kernel's cgroup counters. A workload cannot write them.

Phase 1 makes sure both classes are present and captured. Phase 7 uses the second
to check the first.

## Why the fault catalogue is shaped the way it is

Every experiment is designed so the **loudest symptom is a victim, not the cause**.
The fault-propagation-aware benchmark (FSE 2026) found that on four widely used
public RCA benchmarks the root cause is identifiable by simple correlation, because
the root-cause service's own symptom is the most prominent. On a dataset like that,
any attack that plants a prominent anomaly succeeds for an uninteresting reason.

See `platform/chaos/experiments/README.md` for the per-experiment rationale.

## Derived metrics, and why

The demo app emits only about a dozen OTLP metric points, so Prometheus alone
has nothing to chart. The span stream is rich. The collector therefore runs the
**spanmetrics connector**, deriving per-service rate, error and duration series
from spans:

```
rca_calls_total
rca_duration_milliseconds_{bucket,sum,count}
```

The namespace is pinned to `rca_` so the names are deterministic rather than
dependent on a connector default that has changed across versions. The
provisioned Grafana dashboard queries exactly those names, and the datasource
UIDs are pinned too — a randomly assigned UID makes every panel read
"datasource not found", which looks identical to having no data.

## The tooling, and what each tool refuses to do

Phase 1 produced more diagnostic tooling than planned, because several bugs
presented as healthy systems. Each of these exists to make one specific
ambiguity impossible:

| Command | Makes impossible |
|---|---|
| `make verify` | capturing while the trust boundary is broken |
| `make doctor` | confusing OOMKilled with a rejected config (both show CrashLoopBackOff) |
| `make telemetry` | confusing "the app emits nothing" with "the exporter cannot deliver" |
| `make chaos-check` | a selector that matches zero pods passing as a working experiment |
| `make chaos-smoke` | an injected fault left running after a crash (EXIT trap) |
| preflight in `make install` | a schema error discovered after half the stack has upgraded |

A recurring theme is worth recording for the thesis: three separate times, a
**failed read was reported as a negative result** — a mis-typed Loki marker, a
wrong Tempo port, and an unreachable API server that made `doctor` announce
"no problems found". That is precisely the confusion the project's attack
exploits, encountered accidentally in its own tooling.

## What Phase 1 deliberately does not include

| Not here | Why | Where it lands |
|---|---|---|
| Recorder / snapshot store | needs the tool surface frozen first | P2 |
| Mock tool server | depends on the recorder's format | P2 |
| eBPF observer (OBI) | second independent source; verify OBI 1.0 shipped | P7 |
| Provenance verifier | the contribution; needs captured data to evaluate | P7 |
| Agent scaffolds under test | HolmesGPT et al. attach to the tool API, not the cluster | P1→P5 |
