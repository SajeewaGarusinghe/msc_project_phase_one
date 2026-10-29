#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verify every chaos experiment's selector actually matches running pods.
#
# WHY THIS EXISTS: a label selector that matches nothing is accepted by
# Kubernetes without complaint. The experiment applies "successfully", no fault
# is injected, and the telemetry looks like a healthy system -- so a capture run
# would record a non-incident as an incident. That is the worst possible
# failure mode for this project, because nothing about it looks wrong.
#
# This already happened once: the experiments were written against the demo
# app's older component names (productcatalogservice, cartservice) which the
# current chart renamed (product-catalog, cart).
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_kubeconfig
need kubectl "Install kubectl."
need python3 "Install python3."

FAIL=0
echo
c_ylw "Checking experiment selectors against running pods in 'app'"
echo

for f in "$REPO_ROOT"/platform/chaos/experiments/*.yaml; do
  name="$(basename "$f" .yaml)"

  # Render the selector as a kubectl -l expression.
  SEL="$(python3 -c "
import yaml,sys
d=yaml.safe_load(open('$f'))
ls=(d.get('spec',{}).get('selector',{}) or {}).get('labelSelectors') or {}
print(','.join(f'{k}={v}' for k,v in ls.items()))
")"

  if [ -z "$SEL" ]; then
    c_red "  FAIL  $name -- no labelSelectors"
    FAIL=$((FAIL+1)); continue
  fi

  COUNT="$(kubectl get pods -n app -l "$SEL" --no-headers 2>/dev/null | wc -l)"
  if [ "$COUNT" -gt 0 ]; then
    c_grn "  PASS  $name -> $COUNT pod(s)   [$SEL]"
  else
    c_red  "  FAIL  $name -> 0 pods         [$SEL]"
    FAIL=$((FAIL+1))
  fi
done

if [ "$FAIL" -gt 0 ]; then
  echo
  c_red "$FAIL experiment(s) match no pods. They would apply cleanly and inject nothing."
  echo
  c_ylw "Component labels actually present in the cluster:"
  kubectl get pods -n app -o jsonpath='{range .items[*]}{.metadata.labels.app\.kubernetes\.io/component}{"\n"}{end}' 2>/dev/null \
    | sort -u | sed '/^$/d;s/^/      /'
  exit 1
fi

echo
c_grn "All experiment selectors match running pods."
