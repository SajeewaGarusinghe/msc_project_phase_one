# Port forwarding — manual reference

`make tunnel-*` is a thin wrapper. Everything below is the raw command, so you
can run it yourself, change a port, or background it.

## The commands

```bash
export KUBECONFIG=~/msc_project_phase_one/kubeconfig
```

| What | Command | Then open |
|---|---|---|
| Grafana | `kubectl port-forward -n observability svc/grafana 3000:80` | http://localhost:3000 |
| Astronomy Shop | `kubectl port-forward -n app svc/frontend-proxy 8080:8080` | http://localhost:8080 |
| Prometheus | `kubectl port-forward -n observability svc/prometheus-server 9090:80` | http://localhost:9090 |
| Tempo | `kubectl port-forward -n observability svc/tempo 3200:3200` | http://localhost:3200 |
| Loki | `kubectl port-forward -n observability svc/loki 3100:3100` | http://localhost:3100 |
| Chaos dashboard | `kubectl port-forward -n chaos-mesh svc/chaos-dashboard 2333:2333` | http://localhost:2333 |

Grafana login: `admin` / `admin`.

**Argument order matters.** `kubectl port-forward` takes the resource first and
the mapping second. Reversed, kubectl treats the mapping as a pod name and
reports `pods "3000:80" not found`.

## From Windows, when WSL's localhost does not carry through

```bash
kubectl port-forward --address 0.0.0.0 -n observability svc/grafana 3000:80
```

`kubectl` listens on `127.0.0.1` by default. WSL2 usually bridges that to
Windows, but not when mirrored networking is off or `localhostForwarding` is
disabled. Binding all interfaces always works — and also exposes the port to
your LAN, which is why it is not the default.

Equivalent through the Makefile:

```bash
BIND=0.0.0.0 make tunnel-grafana
```

## Running several at once

Each forward blocks its terminal. To background them:

```bash
kubectl port-forward -n observability svc/grafana 3000:80 >/dev/null 2>&1 &
kubectl port-forward -n app svc/frontend-proxy 8080:8080 >/dev/null 2>&1 &
jobs                 # list them
kill %1 %2           # stop them
```

Or stop every forward at once:

```bash
pkill -f "kubectl port-forward"
```

## When a forward dies on its own

A forward is bound to one **pod**, not the service. If that pod is replaced —
any `make install`, any chaos experiment that kills it — the forward drops with
`lost connection to pod`. That is expected. Re-run the command.

## Quick checks without a browser

Through the API server proxy, so no forward is needed at all:

```bash
# does Tempo hold traces?
kubectl get --raw '/api/v1/namespaces/observability/services/tempo:3200/proxy/api/search?limit=1'

# does Loki have labels?
kubectl get --raw '/api/v1/namespaces/observability/services/loki:3100/proxy/loki/api/v1/labels'

# the span-derived metric names this project provisions
kubectl get --raw '/api/v1/namespaces/observability/services/prometheus-server:80/proxy/api/v1/label/__name__/values' \
  | tr ',' '\n' | grep rca_
```
