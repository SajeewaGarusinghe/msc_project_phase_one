#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# P0 exit test.
#
# Two classes of check:
#   A. LIVENESS  -- is the bench actually working?
#   B. VALIDITY  -- is the trust boundary intact?
#
# Class B is the one that matters for the research. A bench that works but
# leaks the fault injector to the agent produces results that look fine and
# mean nothing. Those checks are therefore FATAL, not warnings.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_kubeconfig
need kubectl "Install kubectl."
require_api 3

PASS=0; FAIL=0
ok()   { c_grn "  PASS  $*"; PASS=$((PASS+1)); }
bad()  { c_red "  FAIL  $*"; FAIL=$((FAIL+1)); }

SA="system:serviceaccount:research:rca-agent"

echo
c_ylw "A. LIVENESS"

for ns in app observability chaos-mesh research; do
  kubectl get ns "$ns" >/dev/null 2>&1 && ok "namespace $ns exists" || bad "namespace $ns missing"
done

# Every pod either Running or Completed (jobs).
# Terminating pods are ignored: a rollout seconds earlier is not a fault, and
# flagging it sends you debugging a pod that is already gone.
NOTREADY="$(kubectl get pods -A --no-headers 2>/dev/null \
  | awk '$4!="Running" && $4!="Completed" && $4!="Terminating" {print $1"/"$2" ("$4")"}' || true)"
if [[ -z "$NOTREADY" ]]; then
  ok "all pods Running/Completed"
else
  bad "pods not ready:"
  echo "$NOTREADY" | sed 's/^/          /'
  # Print WHY, rather than making you go and ask. This is the information you
  # would fetch next in every single case.
  while read -r line; do
    [[ -z "$line" ]] && continue
    NS="${line%%/*}"; REST="${line#*/}"; POD="${REST%% *}"
    echo "          --- $NS/$POD: last container state ---"
    kubectl get pod "$POD" -n "$NS" -o jsonpath='{range .status.containerStatuses[*]}            {.name}: {.state}{"\n"}{end}' 2>/dev/null | head -6
    echo "          --- $NS/$POD: last log lines ---"
    kubectl logs "$POD" -n "$NS" --tail=8 --all-containers 2>&1 | sed 's/^/            /' | head -10
  done <<< "$NOTREADY"
fi

# The three signals must actually be ARRIVING, not merely be queryable. An
# empty backend is the most common silent failure: the stack is "up" but the
# app is exporting nowhere, so every later phase captures nothing.
#
# Probes go through the API server's service proxy rather than `kubectl exec`.
# exec depends on the workload KIND (Deployment vs StatefulSet) and on the
# container image shipping a shell and wget -- neither is guaranteed, and when
# either is wrong the failure is indistinguishable from "no data". The proxy
# needs nothing inside the container.
probe() {
  # probe <ns> <svc> <port> <path>  -> body on stdout, non-zero if unreachable
  kubectl get --raw \
    "/api/v1/namespaces/$1/services/$2:$3/proxy$4" 2>/dev/null
}

# Report "could not reach" and "reached but empty" as DIFFERENT outcomes.
#
# The marker strings are per-API and easy to get wrong -- an earlier revision
# checked Loki's /labels response for "values" when that endpoint actually
# returns {"status":"success","data":[...]}, and reported a perfectly healthy
# Loki as empty. Each check therefore carries BOTH a positive marker and the
# exact body that means "reached it, and it is empty", so a mismatched marker
# shows up as an unrecognised response rather than a false alarm.
check_backend() {
  local label="$1" ns="$2" svc="$3" port="$4" path="$5" marker="$6" empty="$7" hint="$8"
  local body
  body="$(probe "$ns" "$svc" "$port" "$path" || true)"
  if [[ -z "$body" ]]; then
    bad "$label -- PROBE FAILED (could not reach svc/$svc:$port in $ns)"
    echo "          A probe problem, not necessarily a data problem."
    echo "          Check the service exists:  kubectl get svc -n $ns"
    return
  fi
  if [[ -n "$empty" && "$body" == *"$empty"* ]]; then
    bad "$label reached, but holds NO data"
    echo "          $hint"
    return
  fi
  if [[ "$body" == *"$marker"* ]]; then
    ok "$label HAS data"
  else
    bad "$label returned a response this check does not recognise"
    echo "          expected to find: $marker"
    echo "          got: $(echo "$body" | head -c 160)"
  fi
}

check_backend "Tempo" observability tempo 3200 "/api/search?limit=1" \
  'traceID' '"traces":[]' \
  "No traces yet. Give the load generator a minute, or drive traffic with 'make tunnel-frontend'."

PROM="$(probe observability prometheus-server 80 "/api/v1/query?query=count(up)" || true)"
if [[ "$PROM" == *'"status":"success"'* ]]; then
  ok "Prometheus responding"
else
  bad "Prometheus query failed"
fi

check_backend "Loki" observability loki 3100 "/loki/api/v1/labels" \
  '"status":"success"' '"data":[]' \
  "Run 'make telemetry' and read log_records: accepted=0 means the app emits none."

echo
c_ylw "B. VALIDITY -- the trust boundary (fatal if violated)"

# B1: the agent must NOT be able to read chaos resources.
if kubectl auth can-i list networkchaos.chaos-mesh.org --as="$SA" -A 2>/dev/null | grep -qi '^yes'; then
  bad "agent CAN list NetworkChaos -- it can read the answer key"
else
  ok "agent cannot list NetworkChaos cluster-wide"
fi

if kubectl auth can-i get podchaos.chaos-mesh.org --as="$SA" -n chaos-mesh 2>/dev/null | grep -qi '^yes'; then
  bad "agent CAN get PodChaos in chaos-mesh"
else
  ok "agent cannot get PodChaos in chaos-mesh"
fi

# B2: the agent must NOT be able to enumerate namespaces (which would reveal
#     that a chaos-mesh namespace exists at all).
if kubectl auth can-i list namespaces --as="$SA" 2>/dev/null | grep -qi '^yes'; then
  bad "agent CAN list namespaces -- it can discover chaos-mesh"
else
  ok "agent cannot list namespaces"
fi

# B3: the agent must NOT be able to see anything in the chaos namespace.
if kubectl auth can-i list pods --as="$SA" -n chaos-mesh 2>/dev/null | grep -qi '^yes'; then
  bad "agent CAN list pods in chaos-mesh"
else
  ok "agent cannot list pods in chaos-mesh"
fi

# B4: no ClusterRoleBinding may reference the agent.
if kubectl get clusterrolebindings -o json 2>/dev/null \
   | grep -q '"name": *"rca-agent"'; then
  bad "a ClusterRoleBinding references rca-agent"
else
  ok "no ClusterRoleBinding references rca-agent"
fi

# B5: positive control -- the boundary must not be so tight the agent is useless.
if kubectl auth can-i get pods/log --as="$SA" -n app 2>/dev/null | grep -qi '^yes'; then
  ok "agent CAN read pod logs in app (positive control)"
else
  bad "agent cannot read pod logs in app -- it has no evidence to reason over"
fi

echo
if [[ $FAIL -eq 0 ]]; then
  c_grn "P0 exit test PASSED  ($PASS checks)"
else
  c_red "P0 exit test FAILED  ($FAIL failed, $PASS passed)"
  c_red "Do NOT capture incidents until validity checks pass."
  exit 1
fi
