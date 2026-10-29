#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# The real P0 exit test: does an injected fault VISIBLY change all three
# signals?
#
# `make verify` proves the bench is wired up and the trust boundary holds. It
# does not prove the bench can produce an incident. A cluster where chaos
# applies cleanly but changes nothing looks identical to a healthy one -- and
# every captured "incident" would be a non-incident.
#
#   smoke-chaos.sh <experiment-name>
#
# Read-only apart from applying and then deleting the named experiment.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_kubeconfig
need kubectl "Install kubectl."

EXP="${1:-01-network-delay-productcatalog}"
F="$REPO_ROOT/platform/chaos/experiments/${EXP}.yaml"
[[ -f "$F" ]] || die "no such experiment: $EXP"

WINDOW="${WINDOW:-90}"

q_prom() {
  kubectl get --raw \
    "/api/v1/namespaces/observability/services/prometheus-server:80/proxy/api/v1/query?query=$1" 2>/dev/null || true
}
# Total spans Tempo has ingested is a cheap proxy for "is traffic flowing".
count_traces() {
  kubectl get --raw \
    '/api/v1/namespaces/observability/services/tempo:3200/proxy/api/search?limit=20' 2>/dev/null \
    | grep -o '"traceID"' | wc -l
}

ROOT="$(grep -oP 'bench.msc/root-cause:\s*"\K[^"]+' "$F" || echo '?')"
VICTIM="$(grep -oP 'bench.msc/intended-victim:\s*"\K[^"]+' "$F" || echo '?')"

echo
c_ylw "Experiment : $EXP"
echo   "  root cause     : $ROOT"
echo   "  intended victim: $VICTIM"
echo   "  window         : ${WINDOW}s"

echo
c_ylw "1. Baseline (before injection)"
B_TRACES="$(count_traces)"
B_PODS="$(kubectl get pods -n app --no-headers 2>/dev/null | awk '{s+=$4} END{print s+0}')"
echo "  traces in last search page : $B_TRACES"
echo "  total pod restarts in app  : $B_PODS"

echo
c_ylw "2. Injecting"
kubectl apply -f "$F" >/dev/null && c_grn "  applied"

# Confirm the controller actually selected something. A chaos object that
# selected zero targets reports no error at all.
sleep 10
KIND="$(python3 -c "import yaml;print(yaml.safe_load(open('$F'))['kind'].lower())")"
NAME="$(python3 -c "import yaml;print(yaml.safe_load(open('$F'))['metadata']['name'])")"
RECORDS="$(kubectl get "$KIND" "$NAME" -n chaos-mesh -o jsonpath='{.status.experiment.containerRecords[*].id}' 2>/dev/null || true)"
if [[ -n "$RECORDS" ]]; then
  c_grn "  chaos selected target(s): $RECORDS"
else
  c_red "  chaos selected NO targets -- the fault is not being applied"
  c_red "  run 'make chaos-check' and inspect: kubectl describe $KIND $NAME -n chaos-mesh"
fi

echo
c_ylw "3. Waiting ${WINDOW}s for symptoms to propagate"
sleep "$WINDOW"

echo
c_ylw "4. After injection"
A_TRACES="$(count_traces)"
A_PODS="$(kubectl get pods -n app --no-headers 2>/dev/null | awk '{s+=$4} END{print s+0}')"
echo "  traces in last search page : $A_TRACES"
echo "  total pod restarts in app  : $A_PODS  (was $B_PODS)"

echo
c_ylw "5. Signals"
ERRS="$(q_prom 'sum(rate(traces_spanmetrics_calls_total%7Bstatus_code%3D%22STATUS_CODE_ERROR%22%7D%5B2m%5D))' | head -c 200)"
echo "  prometheus error-rate query: ${ERRS:-(no spanmetrics -- expected unless enabled)}"
if [[ "$A_PODS" -gt "$B_PODS" ]]; then
  c_grn "  pod restarts INCREASED -- availability fault visible"
fi

echo
c_ylw "6. Cleaning up"
kubectl delete -f "$F" --ignore-not-found >/dev/null && c_grn "  experiment removed"

echo
c_dim "Now LOOK at it: make tunnel-grafana -> compare the last 10 minutes."
c_dim "P0 is complete when you can SEE the fault in metrics, traces and logs,"
c_dim "and the loudest symptom is the victim rather than the root cause."
