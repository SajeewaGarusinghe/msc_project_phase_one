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

# Count traces SLOWER than a threshold inside an explicit time window.
#
# An earlier revision counted traces with limit=20 and compared 20 to 20, and
# watched pod restarts -- which cannot move for a latency fault. Neither could
# ever show a network delay. Latency is the signal this fault produces, so
# latency is what has to be measured, over a bounded window so "before" does
# not include the whole preceding hour.
SLOW_MS="${SLOW_MS:-250}"
count_slow() {   # count_slow <start-unix> <end-unix>
  kubectl get --raw \
    "/api/v1/namespaces/observability/services/tempo:3200/proxy/api/search?start=$1&end=$2&minDuration=${SLOW_MS}ms&limit=100" \
    2>/dev/null | grep -o '"traceID"' | wc -l
}
count_any() {    # count_any <start-unix> <end-unix>
  kubectl get --raw \
    "/api/v1/namespaces/observability/services/tempo:3200/proxy/api/search?start=$1&end=$2&limit=100" \
    2>/dev/null | grep -o '"traceID"' | wc -l
}

ROOT="$(grep -oP 'bench.msc/root-cause:\s*"\K[^"]+' "$F" || echo '?')"
VICTIM="$(grep -oP 'bench.msc/intended-victim:\s*"\K[^"]+' "$F" || echo '?')"

echo
c_ylw "Experiment : $EXP"
echo   "  root cause     : $ROOT"
echo   "  intended victim: $VICTIM"
echo   "  window         : ${WINDOW}s"

echo
c_ylw "1. Baseline (the ${WINDOW}s BEFORE injection)"
B_END=$(date +%s); B_START=$(( B_END - WINDOW ))
B_SLOW="$(count_slow "$B_START" "$B_END")"
B_ANY="$(count_any "$B_START" "$B_END")"
B_PODS="$(kubectl get pods -n app --no-headers 2>/dev/null | awk '{s+=$4} END{print s+0}')"
printf "  traces: %s total, %s slower than %sms\n" "$B_ANY" "$B_SLOW" "$SLOW_MS"
echo   "  pod restarts in app: $B_PODS"
if [[ "$B_ANY" -eq 0 ]]; then
  c_red "  No traffic at all. The load generator is not driving the app --"
  c_red "  nothing can be measured. Check: kubectl get pods -n app | grep load"
fi

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

INJECT_AT=$(date +%s)
echo
c_ylw "3. Waiting ${WINDOW}s for symptoms to propagate"
sleep "$WINDOW"

echo
c_ylw "4. After injection (the ${WINDOW}s SINCE)"
A_END=$(date +%s)
A_SLOW="$(count_slow "$INJECT_AT" "$A_END")"
A_ANY="$(count_any "$INJECT_AT" "$A_END")"
A_PODS="$(kubectl get pods -n app --no-headers 2>/dev/null | awk '{s+=$4} END{print s+0}')"
printf "  traces: %s total, %s slower than %sms\n" "$A_ANY" "$A_SLOW" "$SLOW_MS"
echo   "  pod restarts in app: $A_PODS  (was $B_PODS)"

echo
c_ylw "5. Verdict"
VISIBLE=0
if [[ "$A_SLOW" -gt "$B_SLOW" ]]; then
  c_grn "  LATENCY: slow traces rose $B_SLOW -> $A_SLOW   (fault is visible)"
  VISIBLE=1
else
  c_ylw "  LATENCY: slow traces $B_SLOW -> $A_SLOW (no rise)"
fi
if [[ "$A_PODS" -gt "$B_PODS" ]]; then
  c_grn "  AVAILABILITY: pod restarts rose $B_PODS -> $A_PODS"
  VISIBLE=1
fi
if [[ "$VISIBLE" -eq 0 ]]; then
  c_red "  No measurable change."
  echo  "    - a latency fault needs minDuration BELOW the injected delay;"
  echo  "      this run used SLOW_MS=$SLOW_MS. Try: SLOW_MS=150 make chaos-smoke EXP=$EXP"
  echo  "    - or the delay is not reaching the caller. Inspect:"
  echo  "      kubectl describe $KIND $NAME -n chaos-mesh"
fi

echo
c_ylw "6. Cleaning up"
kubectl delete -f "$F" --ignore-not-found >/dev/null && c_grn "  experiment removed"

echo
c_dim "Now LOOK at it: make tunnel-grafana -> compare the last 10 minutes."
c_dim "P0 is complete when you can SEE the fault in metrics, traces and logs,"
c_dim "and the loudest symptom is the victim rather than the root cause."
