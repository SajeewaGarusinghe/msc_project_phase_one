#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Port-forward the observability UIs to localhost.
#
# Nothing in platform/ is exposed by a Service of type LoadBalancer or an
# Ingress -- that is deliberate. Everything is reached through this tunnel, so
# the bench never has an open port beyond the k3s API.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_kubeconfig
need kubectl "Install kubectl."

declare -A PORTS=(
  [grafana]="3000:80 -n observability svc/grafana"
  [prometheus]="9090:9090 -n observability svc/prometheus-server"
  [tempo]="3200:3100 -n observability svc/tempo"
  [loki]="3100:3100 -n observability svc/loki"
  [frontend]="8080:8080 -n app svc/frontend-proxy"
  [chaos]="2333:2333 -n chaos-mesh svc/chaos-dashboard"
)

WHAT="${1:-grafana}"
[[ -n "${PORTS[$WHAT]:-}" ]] || die "usage: tunnel.sh {${!PORTS[*]}}"

c_grn "Forwarding $WHAT -- Ctrl-C to stop"
# shellcheck disable=SC2086
exec kubectl port-forward ${PORTS[$WHAT]}
