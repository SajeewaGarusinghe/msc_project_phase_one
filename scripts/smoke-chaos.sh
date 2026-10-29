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
require_api 3

EXP="${1:-01-network-delay-productcatalog}"
F="$REPO_ROOT/platform/chaos/experiments/${EXP}.yaml"
[[ -f "$F" ]] || die "no such experiment: $EXP"

# A crash between inject and cleanup leaves the fault running on the cluster,
# silently poisoning everything measured afterwards. An earlier run did exactly
# that. The trap fires on normal exit, on error and on Ctrl-C.
cleanup() {
  kubectl delete -f "$F" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Measurement must never kill the run: a failed query is a missing number, not
# a reason to abandon an injected fault. pipefail in particular turns one slow
# kubectl into a script-ending error.
set +e
set +o pipefail

WINDOW="${WINDOW:-90}"

# Measure the trace-duration DISTRIBUTION over an explicit window.
#
# Two earlier revisions failed here. The first counted traces with limit=20 and
# compared 20 to 20. The second counted traces slower than a fixed 250ms, but
# 95% of this app's traces are already slower than that at rest, and the count
# saturated at the page limit -- so "95 -> 100" was a capped number moving, not
# latency rising. A count above an arbitrary threshold cannot discriminate when
# the threshold sits below ambient latency.
#
# Percentiles of the actual durations have neither problem.
LIMIT="${LIMIT:-1000}"

durations() {   # durations <start-unix> <end-unix>  ->  one duration (ms) per line
  { kubectl get --raw \
    "/api/v1/namespaces/observability/services/tempo:3200/proxy/api/search?start=$1&end=$2&limit=${LIMIT}" \
    2>/dev/null || true; } | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for t in d.get('traces', []):
    print(t.get('durationMs', 0))
"
}

stats() {       # stats <label>  (reads durations on stdin)
  python3 -c "
import sys
v = sorted(int(x) for x in sys.stdin.read().split() if x.strip().isdigit())
if not v:
    print('  $1: no traces in window'); sys.exit(0)
def pct(p):
    return v[min(len(v) - 1, int(len(v) * p / 100))]
sat = ' (SATURATED at the page limit -- raise LIMIT)' if len(v) >= ${LIMIT} else ''
print(f'  $1: n={len(v)}{sat}  p50={pct(50)}ms  p95={pct(95)}ms  max={v[-1]}ms')
print(f'__P95__ {pct(95)}')
"
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
B_OUT="$(durations "$B_START" "$B_END" | stats baseline)"
echo "$B_OUT" | grep -v '^__P95__'
B_P95="$(echo "$B_OUT" | awk '/^__P95__/{print $2}')"
B_PODS="$(kubectl get pods -n app --no-headers 2>/dev/null | awk '{s+=$4} END{print s+0}')"
echo "  pod restarts in app: $B_PODS"
if [[ -z "$B_P95" ]]; then
  c_red "  No traffic. The load generator is not driving the app, so nothing"
  c_red "  can be measured. Check: kubectl get pods -n app | grep load"
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
A_OUT="$(durations "$INJECT_AT" "$A_END" | stats after)"
echo "$A_OUT" | grep -v '^__P95__'
A_P95="$(echo "$A_OUT" | awk '/^__P95__/{print $2}')"
A_PODS="$(kubectl get pods -n app --no-headers 2>/dev/null | awk '{s+=$4} END{print s+0}')"
echo "  pod restarts in app: $A_PODS  (was $B_PODS)"

echo
c_ylw "5. Verdict"
VISIBLE=0
if [[ -n "$B_P95" && -n "$A_P95" ]]; then
  DELTA=$(( A_P95 - B_P95 ))
  if [[ "$DELTA" -gt 50 ]]; then
    c_grn "  LATENCY: p95 rose ${B_P95}ms -> ${A_P95}ms  (+${DELTA}ms)"
    VISIBLE=1
  else
    c_ylw "  LATENCY: p95 ${B_P95}ms -> ${A_P95}ms  (change ${DELTA}ms, within noise)"
  fi
fi
if [[ "$A_PODS" -gt "$B_PODS" ]]; then
  c_ylw "  RESTARTS: $B_PODS -> $A_PODS. Which pod, and was it the target?"
  kubectl get pods -n app --no-headers 2>/dev/null \
    | awk '$4>0 {printf "            %s  restarts=%s\n",$1,$4}'
  echo  "            A restart under a LATENCY fault usually means a liveness"
  echo  "            probe timed out. That is a real symptom, but it is a"
  echo  "            different fault class than the one being injected --"
  echo  "            worth knowing before you treat the scenario as clean."
fi
if [[ "$VISIBLE" -eq 0 ]]; then
  c_red "  No clear latency change."
  echo  "    - the fault may not reach the CALLER. This experiment delays"
  echo  "      traffic TO product-catalog, so the rise shows in its callers."
  echo  "    - inspect: kubectl describe $KIND $NAME -n chaos-mesh"
  echo  "    - or widen the window: WINDOW=180 make chaos-smoke EXP=$EXP"
fi

echo
c_ylw "6. Cleaning up"
cleanup && c_grn "  experiment removed (the EXIT trap also guarantees this)"

echo
c_dim "Now LOOK at it: make tunnel-grafana -> compare the last 10 minutes."
c_dim "P0 is complete when you can SEE the fault in metrics, traces and logs,"
c_dim "and the loudest symptom is the victim rather than the root cause."
