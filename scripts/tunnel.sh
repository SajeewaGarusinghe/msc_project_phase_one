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

# kubectl port-forward takes the RESOURCE first, then the port mapping:
#     kubectl port-forward -n NS svc/NAME LOCAL:REMOTE
# An earlier revision had these reversed, so kubectl read "3000:80" as a pod
# name and reported `pods "3000:80" not found`.
declare -A TARGET=(
  [grafana]="-n observability svc/grafana"
  [prometheus]="-n observability svc/prometheus-server"
  [tempo]="-n observability svc/tempo"
  [loki]="-n observability svc/loki"
  [frontend]="-n app svc/frontend-proxy"
  [chaos]="-n chaos-mesh svc/chaos-dashboard"
)
declare -A MAP=(
  [grafana]="3000:80"
  [prometheus]="9090:80"
  [tempo]="3200:3200"
  [loki]="3100:3100"
  [frontend]="8080:8080"
  [chaos]="2333:2333"
)

WHAT="${1:-grafana}"
[[ -n "${TARGET[$WHAT]:-}" ]] || die "usage: tunnel.sh {${!TARGET[*]}}"

c_grn "Forwarding $WHAT on localhost:${MAP[$WHAT]%%:*} -- Ctrl-C to stop"
# shellcheck disable=SC2086
exec kubectl port-forward ${TARGET[$WHAT]} ${MAP[$WHAT]}
