# Runbook

## First run — local (free)

```bash
# Needs Docker with >= 12 GB allocated (20 GB comfortable)
make local-up
make install TARGET=local
make verify  TARGET=local
```

## First run — cloud

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars

# REQUIRED: lock access to your own IP
curl -s https://checkip.amazonaws.com     # -> put this in allowed_cidr as x.x.x.x/32
$EDITOR terraform.tfvars

cd ../..
make tf-init && make tf-plan      # READ THE PLAN
make tf-apply

# cloud-init needs ~3 minutes to install k3s after apply returns
make kubeconfig
make install
make verify
```

Then pin the chart versions so the bench is reproducible:

```bash
make pin-versions
git add platform/versions.env && git commit -m "chore: pin chart versions"
```

## Daily loop

```bash
make start                  # cloud only
make kubeconfig             # the API address is stable, but refresh after a rebuild
export KUBECONFIG=$PWD/kubeconfig

make chaos-list
make chaos-apply EXP=01-network-delay-productcatalog
make tunnel-grafana         # localhost:3000, admin/admin — watch it propagate
make chaos-delete EXP=01-network-delay-productcatalog

make stop                   # ALWAYS
```

## Verifying an experiment is usable

Before an incident is worth capturing, confirm in Grafana that:

1. the **root-cause service** shows the fault you injected;
2. at least one **victim** shows a symptom that is *at least as prominent*;
3. all three signals moved — metrics, traces and logs.

If the cause is the loudest thing on the dashboard, the scenario is too easy and
will not discriminate between a real diagnosis and a prominence heuristic. Reshape
it before capturing. See `platform/chaos/experiments/README.md`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pods stuck `Pending` | node out of memory | bigger `instance_type`, or raise Docker's limit locally |
| Pods crash-loop, "too many open files" | inotify limits | cloud-init sets these; locally raise `fs.inotify.max_user_*` on the **host** |
| Chaos experiments apply but nothing happens | wrong containerd socket | check `chaosDaemon.socketPath` matches k3s (`/run/k3s/containerd/containerd.sock`) |
| Tempo empty, app healthy | collector endpoint wrong | check `OTEL_EXPORTER_OTLP_ENDPOINT` in the app values resolves |
| Loki rejects planted records | timestamp outside window | `reject_old_samples: false` — already set in `loki.yaml` |
| `make kubeconfig` times out | node stopped, or SSM agent not up | `make start`, wait 60 s, retry |
| `make verify` fails on a B-check | trust boundary broken | **stop. Do not capture.** See [03-trust-boundary.md](03-trust-boundary.md) |
| Node stopped itself overnight | auto-stop alarm fired | expected — `make start` |

## Node shell without opening a port

```bash
aws ssm start-session --target "$(cd infra/terraform && terraform output -raw instance_id)"
# on the node:
bench-info
```

## Tearing down

```bash
make uninstall       # platform only, keeps the cluster
make stop            # cloud: stop billing compute
make tf-destroy      # cloud: remove everything including the volume
make local-destroy   # local
```

Run `make tf-destroy` whenever the next phase does not need a cluster — a stopped
instance still bills for its root volume.
