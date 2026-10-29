#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Follow the telemetry path end to end and say exactly where it stops.
#
#   app service  ->  OTel Collector  ->  Tempo / Prometheus / Loki
#
# Run when verify reports a backend holds no data.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# A diagnostic must never abort partway and hide the step that mattered.
set +e
require_kubeconfig
need kubectl "Install kubectl."

echo
c_ylw "1. What the app is configured to export to"
DEP="$(kubectl get deploy -n app -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
if [ -n "$DEP" ]; then
  echo "   (sampling deployment: $DEP)"
  kubectl exec -n app "deploy/$DEP" -- env 2>/dev/null \
    | grep -E '^OTEL_(EXPORTER|TRACES|LOGS|METRICS)' | sed 's/^/     /' \
    || echo "     (could not read env from the container)"
else
  c_red "   no deployments found in namespace app"
fi

echo
c_ylw "2. Does that endpoint resolve to a real service?"
kubectl get svc -n observability -o custom-columns=NAME:.metadata.name,PORTS:.spec.ports[*].port 2>/dev/null | sed 's/^/     /'

echo
c_ylw "3. Is the collector receiving and exporting?"
COL="$(kubectl get deploy -n observability -o name 2>/dev/null | grep -i collector | head -1)"
if [ -n "$COL" ]; then
  echo "   ($COL)"
  kubectl logs -n observability "$COL" --tail=25 2>/dev/null \
    | grep -Ei 'error|refused|failed|permanent|dropped|exporter' | tail -12 | sed 's/^/     /' \
    || echo "     no errors in the last 25 log lines (good)"
else
  c_red "   no collector deployment found in observability"
fi

echo
c_ylw "4. Are the backends reachable through the API server proxy?"
for spec in "tempo:3200:/ready" "loki:3100:/ready" "prometheus-server:80:/-/ready"; do
  svc="${spec%%:*}"; rest="${spec#*:}"; port="${rest%%:*}"; path="${rest#*:}"
  if kubectl get --raw "/api/v1/namespaces/observability/services/${svc}:${port}/proxy${path}" >/dev/null 2>&1; then
    c_grn "     $svc:$port reachable"
  else
    c_red "     $svc:$port NOT reachable via proxy"
  fi
done

echo
c_ylw "5. Has any trace arrived at all?"
BODY="$(kubectl get --raw '/api/v1/namespaces/observability/services/tempo:3200/proxy/api/search?limit=1' 2>/dev/null || true)"
if [ -z "$BODY" ]; then
  c_red "     probe failed (could not reach tempo)"
else
  echo "     $(echo "$BODY" | head -c 200)"
fi

echo
c_dim "Read it as: step 1 shows where the app sends telemetry; step 2 shows"
c_dim "whether that name exists; step 3 shows whether the collector forwarded it."
