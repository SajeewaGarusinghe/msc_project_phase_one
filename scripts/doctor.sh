#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Automatic diagnosis. Finds every unhealthy pod and says WHY, including the
# reason the last container exited -- OOMKilled is invisible in `get pods`,
# which shows only CrashLoopBackOff, and the two need opposite fixes.
#
# Read-only. Run any time; run automatically when `make install` fails.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
set +e
set +o pipefail
require_kubeconfig
need kubectl "Install kubectl."
# Without this, an unreachable API makes every section below print nothing and
# the script conclude "No problems found" -- the most misleading output it
# could possibly produce.
require_api 3

FOUND=0

echo
c_ylw "=== unhealthy pods ==="
BAD="$(kubectl get pods -A --no-headers 2>/dev/null \
  | awk '$3!="Running" && $3!="Completed" && $4!="Running" && $4!="Completed" {print $1" "$2}' \
  | sort -u)"

# Also catch pods that are Running but with a container not ready (0/1, 1/2 ...)
NOTREADY="$(kubectl get pods -A --no-headers 2>/dev/null \
  | awk '{split($3,r,"/"); if (r[1]!=r[2] && $4!="Completed") print $1" "$2}' | sort -u)"
BAD="$(printf '%s\n%s\n' "$BAD" "$NOTREADY" | sed '/^$/d' | sort -u)"

if [[ -z "$BAD" ]]; then
  c_grn "  none -- every pod is Running and ready"
else
  while read -r NS POD; do
    [[ -z "$NS" ]] && continue
    FOUND=1
    echo
    c_red "  $NS/$POD"

    # Why did the previous container die? This is the decisive field.
    kubectl get pod "$POD" -n "$NS" -o json 2>/dev/null | python3 -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
for c in d.get('status', {}).get('containerStatuses', []) or []:
    st  = c.get('state', {})
    lst = c.get('lastState', {}).get('terminated', {})
    cur = next(iter(st), '?')
    print(f\"      container {c['name']}: {cur}  restarts={c.get('restartCount',0)}\")
    if st.get('waiting', {}).get('reason'):
        print(f\"        waiting: {st['waiting']['reason']} {st['waiting'].get('message','')[:120]}\")
    if lst:
        reason = lst.get('reason', '?')
        print(f\"        last exit: {reason}  code={lst.get('exitCode')}\")
        if reason == 'OOMKilled':
            print('        >>> OOMKilled: the MEMORY LIMIT is too low for this workload.')
            print('        >>> Raise resources.limits.memory in the values file, not the request.')
"
    LOGS="$(kubectl logs "$POD" -n "$NS" --tail=20 --all-containers 2>&1)"
    if [[ "$LOGS" == *"is waiting to start"* ]]; then
      echo "      (container has not started yet -- still pulling or scheduling)"
    else
      echo "      --- last 20 log lines ---"
      echo "$LOGS" | sed 's/^/        /'
    fi

    PREV="$(kubectl logs "$POD" -n "$NS" --previous --tail=20 --all-containers 2>/dev/null)"
    if [[ -n "$PREV" ]]; then
      echo "      --- last 20 lines from the PREVIOUS (crashed) container ---"
      echo "$PREV" | sed 's/^/        /'
    fi
  done <<< "$BAD"
fi

echo
c_ylw "=== node pressure ==="
kubectl describe node 2>/dev/null \
  | grep -E 'MemoryPressure|DiskPressure|PIDPressure' | sed 's/^/  /'
kubectl describe node 2>/dev/null \
  | grep -A6 'Allocated resources' | sed 's/^/  /'

echo
c_ylw "=== memory budget ==="
# Over-commitment is the root cause behind most OOMKills here, and it is
# invisible until something dies. 95% of the ceiling means the next pod to
# spike gets killed -- and which pod that is, is luck.
kubectl describe node 2>/dev/null | awk '
  /Allocated resources/ {inres=1}
  inres && /^  memory/ {print "  node memory  requests="$2" limits="$3; inres=0}
'
PCT="$(kubectl describe node 2>/dev/null | grep -A6 'Allocated resources' \
  | grep -oE 'memory[[:space:]]+[0-9]+Mi \(([0-9]+)%\)[[:space:]]+[0-9]+Mi \(([0-9]+)%\)' \
  | grep -oE '\(([0-9]+)%\)' | tail -1 | tr -d '()%')"
if [[ -n "$PCT" ]] && (( PCT >= 90 )); then
  c_red "  memory limits are at ${PCT}% of the node -- OOMKills are expected here."
  echo  "    On WSL the cheapest fix is to give WSL more of the machine:"
  echo  "        make wsl-memory MEM=24GB     (then: wsl --shutdown)"
  echo  "    Otherwise disable more demo components in"
  echo  "        platform/app/values/astronomy-shop.yaml"
elif [[ -n "$PCT" ]]; then
  c_grn "  memory limits at ${PCT}% of the node"
fi

echo
c_ylw "=== recent warning events ==="
kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp --no-headers 2>/dev/null \
  | grep -Ev 'hides previous definition' | tail -10 | sed 's/^/  /'

echo
if [[ "$FOUND" -eq 1 ]]; then
  c_red "Unhealthy pods found. Read 'last exit' above before changing anything:"
  echo  "  OOMKilled      -> raise the memory LIMIT in the relevant values file"
  echo  "  Error / code>0 -> a config the component rejected; the previous"
  echo  "                    container's logs say which"
  echo  "  CreateContainerConfigError -> a missing ConfigMap or Secret"
  exit 1
fi
c_grn "No problems found."
