# ADR-001 — Where to run the bench

**Status:** accepted · **Date:** 2026-10-28 · **Decides:** AWS free tier vs GCP free tier vs local

## Decision

**Dual-target, one runtime.** Run k3s everywhere:

- **`TARGET=local`** — k3d on your own machine. Free. The default for day-to-day work.
- **`TARGET=cloud`** — a *single EC2 instance* running k3s, created by Terraform,
  **stopped between sessions**. For capture runs and for when the laptop is too small.

Both run the same k3s distribution, so `platform/` applies unchanged to either.

**Rejected: managed Kubernetes (EKS or GKE) in any form.**

## The question behind the question

"Which free tier?" has an uncomfortable answer: **there is no free tier for this
workload on any cloud.**

| Component | Documented need |
|---|---|
| Astronomy Shop | ~6 GB RAM |
| Prometheus + Tempo + Loki + Collector | ~3–4 GB |
| k3s + OS | ~1–2 GB |
| Chaos Mesh | ~0.5 GB |
| **Total** | **~12 GB minimum, 16–32 GB realistic** |

Against that:

- **AWS free tier** is `t3.micro` — 1 GB RAM. Off by more than an order of magnitude.
- **GCP free tier** is `e2-micro` — 1 GB RAM. Same.
- **EKS** charges ~$0.10/hr for the control plane alone (~$73/month) and is *never*
  free, before a single worker node.
- **GKE** gives one zonal cluster's management fee free, but nodes still cost money.

So the real choice is **free locally** or **paid-from-credits in the cloud**. The free
tier is not a third option; it is a distraction.

## Why not managed Kubernetes — three project-specific reasons

This is not a general preference. For *this* project, EKS/GKE are actively worse:

1. **Chaos Mesh needs a privileged DaemonSet.** `chaos-daemon` runs privileged on
   every node to manipulate network namespaces and cgroups. **GKE Autopilot refuses
   to schedule it at all.**
2. **Phase 7 needs kernel access.** The eBPF observer that provides the independent
   corroboration signal requires a privileged DaemonSet and a BTF-enabled kernel.
   Managed node pools make this harder; Autopilot makes it impossible.
3. **A control plane you pay for buys nothing here.** There is one node. High
   availability, managed upgrades and multi-AZ scheduling are irrelevant to a
   single-node research bench, and the control-plane fee is pure credit burn.

A disposable single VM is also simply **more reproducible**: `terraform destroy`
then `terraform apply` yields a byte-identical starting point, which matters when
the thesis claims deterministic replay.

## Why k3d locally rather than minikube or kind

The brief asked specifically about minikube. k3d wins on one argument that
outweighs the rest:

> **k3d runs the same k3s distribution as the cloud node.** Same containerd socket
> path, same default `local-path` StorageClass, same disabled components.

That means **one** `platform/` directory works on both targets. With minikube
(different runtime config, extra VM layer on macOS/Windows) or kind (upstream
Kubernetes, not k3s) you would maintain two platform configurations and local
development would drift from the environment where incidents are actually captured
— exactly the kind of difference that produces an irreproducible result six months
later.

## Cost, concretely

Compute only, approximate on-demand rates — **verify current pricing, these move**:

| Option | Spec | Running 24/7 | Realistic use (~6 h/day) |
|---|---|---|---|
| Local k3d | your machine | **$0** | **$0** |
| `t3.xlarge` | 4 vCPU / 16 GB | ~$120/mo | ~$30/mo |
| `t3.2xlarge` | 8 vCPU / 32 GB | ~$240/mo | ~$60/mo |
| Spot `t3.2xlarge` | 8 vCPU / 32 GB | ~$70/mo | ~$18/mo |
| EKS control plane | — | ~$73/mo | ~$73/mo (always on) |

Plus ~$8/month for a 100 GB gp3 root volume, which bills **even while stopped**.
That is the floor, and it is the price of being able to stop rather than rebuild.

### The architectural saving

The project's capture-once-replay-many design (roadmap P2) means **the cluster is
only needed while capturing incidents**. Phase 5 runs thousands of agent trials
against frozen snapshots with **no cluster running at all**. Cloud cost is therefore
bounded by the capture window — days, not months — regardless of how long the
analysis takes.

## Which cloud, if you use one

Use whichever you already have credits for. If starting from nothing:

- **GCP** — the $300 / 90-day trial is the easiest meaningful credit to obtain, and
  `e2-standard-8` (8 vCPU / 32 GB) is slightly cheaper than the AWS equivalent.
- **AWS** — chosen for the Terraform in this repo because the SSM Session Manager
  path gives a shell with **no inbound port open at all**, which is a materially
  better security posture for a bench that will sit on a public IP.

As an MSc student, apply for academic credits before spending your own money:
GitHub Student Developer Pack, AWS academic programmes, and Google Cloud research
credits all cover workloads of this size.

> **Porting to GCP:** only `infra/terraform/` is AWS-specific. `platform/`,
> `scripts/` and the whole k3s layer are portable unchanged. A sibling
> `infra/terraform-gcp/` creating one `e2-standard-8` with the same cloud-init is
> roughly a day's work.

## Recommendation, in order

1. **If your machine has ≥ 16 GB free for Docker** — work locally. Free, fastest
   iteration, full kernel access for Phase 7.
2. **Otherwise** — cloud bench at `t3.xlarge`, `make stop` after every session.
3. **For the Phase 2 capture runs specifically** — cloud, `t3.2xlarge`,
   `use_spot = false`. A Spot reclamation mid-incident corrupts a snapshot, and
   re-capturing costs more than the on-demand premium.

## Consequences

- Two bring-up paths to keep working (`make local-up` vs `make tf-apply`), mitigated
  by both converging on the same `make install`.
- The AWS Terraform is the only cloud-specific code; a GCP port is a known, bounded task.
- Spot is available but defaults to off, because the failure mode it introduces is
  silent data corruption rather than an obvious outage.
