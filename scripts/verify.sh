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
NOTREADY="$(kubectl get pods -A --no-headers 2>/dev/null \
  | awk '$4!="Running" && $4!="Completed" {print $1"/"$2" ("$4")"}' || true)"
if [[ -z "$NOTREADY" ]]; then
  ok "all pods Running/Completed"
else
  bad "pods not ready:"; echo "$NOTREADY" | sed 's/^/          /'
fi

# The three signals must actually be arriving. An empty backend is the most
# common silent failure: the stack is "up" but the app is exporting nowhere.
TRACE_SVCS="$(kubectl exec -n observability deploy/tempo -- \
  wget -qO- 'http://localhost:3100/api/search/tags' 2>/dev/null | head -c 200 || true)"
[[ -n "$TRACE_SVCS" ]] && ok "Tempo responding" || bad "Tempo returned nothing"

PROM_UP="$(kubectl exec -n observability deploy/prometheus-server -c prometheus-server -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=up' 2>/dev/null | head -c 80 || true)"
[[ "$PROM_UP" == *'"status":"success"'* ]] && ok "Prometheus responding" || bad "Prometheus query failed"

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
