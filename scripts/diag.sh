#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# What is the cluster doing right now? Run this in a SECOND shell while
# `make install` is still going -- it is read-only and safe to run any time.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_kubeconfig
need kubectl "Install kubectl."

echo
c_ylw "=== pods by phase ==="
kubectl get pods -A --no-headers 2>/dev/null \
  | awk '{print $4}' | sort | uniq -c | sort -rn | sed 's/^/  /'

echo
c_ylw "=== anything not Running/Completed ==="
NOT="$(kubectl get pods -A --no-headers 2>/dev/null \
  | awk '$4!="Running" && $4!="Completed" {printf "  %-15s %-40s %-20s restarts=%s\n",$1,$2,$4,$5}')"
if [ -n "$NOT" ]; then echo "$NOT"; else c_grn "  (none -- everything is up)"; fi

echo
c_ylw "=== images still pulling ==="
PULL="$(kubectl get events -A --field-selector reason=Pulling --no-headers 2>/dev/null | tail -5)"
if [ -n "$PULL" ]; then echo "$PULL" | sed 's/^/  /'; else echo "  (no active pulls)"; fi

echo
c_ylw "=== recent warnings ==="
kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp --no-headers 2>/dev/null \
  | grep -Ev 'hides previous definition' | tail -8 | sed 's/^/  /'

echo
c_ylw "=== node allocation ==="
kubectl describe node 2>/dev/null \
  | grep -E 'MemoryPressure|Allocated resources' -A4 | head -12 | sed 's/^/  /'

echo
c_ylw "=== is telemetry actually arriving? ==="
T=$(kubectl exec -n observability deploy/tempo -- wget -qO- 'http://localhost:3100/api/search?limit=1' 2>/dev/null | head -c 150)
if [ -n "$T" ]; then echo "  tempo : $T"; else echo "  tempo : (no response yet)"; fi
