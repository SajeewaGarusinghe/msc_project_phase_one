#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Install the P0 platform layer onto an existing k3s cluster.
#
# Ordering is load-bearing:
#   1. namespaces        -- everything else targets them
#   2. observability     -- the collector must exist BEFORE the app starts, or
#                           early spans are dropped and the first incident in
#                           every capture is missing its opening telemetry
#   3. application       -- configured to export to OUR collector
#   4. chaos             -- isolated namespace, installed after the app so its
#                           webhook does not race the app's pod creation
#   5. rbac              -- the agent identity, applied last so it can be
#                           verified against resources that already exist
#
# Idempotent: every step is `helm upgrade --install` or `kubectl apply`.
#
# Each helm step uses --wait, so the script BLOCKS until that release's pods are
# ready. On a first install that is mostly image pulls; on an upgrade it is a
# rollout. Either way it can sit silently for minutes and look hung.
#
# Watch it from a second shell:   make diag
# Shorten the wait:               HELM_TIMEOUT=3m make install
# Skip waiting entirely:          WAIT=0 make install   (then poll with make diag)
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_kubeconfig
# 30s of patience here beats a confusing "connection reset by peer" on step 1.
wait_for_api 15 || require_api 1
need helm "Install Helm 3: https://helm.sh/docs/intro/install/"
need kubectl "Install kubectl."

# shellcheck source=/dev/null
source "$REPO_ROOT/platform/versions.env"
P="$REPO_ROOT/platform"

# Turn an empty version into no flag at all, a pinned version into --version X.
ver_flag() { [[ -z "${1:-}" ]] && echo "" || echo "--version $1"; }

# When a helm step fails -- almost always because a pod never became ready --
# the useful information is in that pod, not in helm's "context deadline
# exceeded". Diagnose automatically rather than making the operator go looking.
on_fail() {
  local rc=$?
  [[ $rc -eq 0 ]] && return 0
  echo
  c_red "A helm step failed (exit $rc). Diagnosing automatically:"
  "$REPO_ROOT/scripts/doctor.sh" || true
  exit $rc
}
trap on_fail EXIT

HELM_TIMEOUT="${HELM_TIMEOUT:-10m}"
if [[ "${WAIT:-1}" == "0" ]]; then
  WAIT_FLAGS=()
  c_ylw "WAIT=0: not waiting for readiness. Poll with 'make diag'."
else
  WAIT_FLAGS=(--wait --timeout "$HELM_TIMEOUT")
fi

c_ylw "[1/6] namespaces"
kubectl apply -f "$P/namespaces.yaml"

c_ylw "[2/6] helm repositories"
helm repo add open-telemetry "$REPO_OTEL" >/dev/null 2>&1 || true
helm repo add prometheus-community "$REPO_PROMETHEUS" >/dev/null 2>&1 || true
helm repo add grafana "$REPO_GRAFANA" >/dev/null 2>&1 || true
helm repo add chaos-mesh "$REPO_CHAOS_MESH" >/dev/null 2>&1 || true
helm repo update >/dev/null

# ---------------------------------------------------------------------------
# Validate every values file against its chart BEFORE touching the cluster.
#
# `helm template` applies the chart's own values.schema.json and renders the
# manifests, without contacting the API server. It catches exactly the errors
# that otherwise surface five minutes into an install, after half the stack has
# already been upgraded: unknown keys, missing required properties, bad
# indentation.
#
# Costs about twenty seconds. The alternative cost it has already avoided:
# `at '/default': additional properties 'resources' not allowed` and
# `at '/components/flagd-ui': missing property 'useDefault'`, both discovered
# only at step 4 of 5.
# ---------------------------------------------------------------------------
c_ylw "[3/6] validating values against chart schemas"
PREFLIGHT_FAIL=0
preflight() {   # preflight <release> <chart> <values-file> [version]
  local out
  if ! out="$(helm template "$1" "$2" -f "$3" $(ver_flag "${4:-}") 2>&1 >/dev/null)"; then
    c_red "  FAIL  $1 ($2)"
    echo "$out" | sed 's/^/        /' | head -12
    PREFLIGHT_FAIL=1
  else
    c_grn "  ok    $1"
  fi
}
preflight prometheus     prometheus-community/prometheus        "$P/observability/values/prometheus.yaml"     "$CHART_VERSION_PROMETHEUS"
preflight tempo          grafana/tempo                          "$P/observability/values/tempo.yaml"          "$CHART_VERSION_TEMPO"
preflight loki           grafana/loki                           "$P/observability/values/loki.yaml"           "$CHART_VERSION_LOKI"
preflight otel-collector open-telemetry/opentelemetry-collector "$P/observability/values/otel-collector.yaml" "$CHART_VERSION_OTEL_COLLECTOR"
preflight grafana        grafana/grafana                        "$P/observability/values/grafana.yaml"        "$CHART_VERSION_GRAFANA"
preflight astronomy      open-telemetry/opentelemetry-demo      "$P/app/values/astronomy-shop.yaml"           "$CHART_VERSION_OTEL_DEMO"
preflight chaos-mesh     chaos-mesh/chaos-mesh                  "$P/chaos/values/chaos-mesh.yaml"             "$CHART_VERSION_CHAOS_MESH"

if [[ "$PREFLIGHT_FAIL" -eq 1 ]]; then
  echo
  c_red "Values rejected by a chart. Nothing was changed on the cluster."
  c_red "Fix the values file above and re-run; no partial install to unwind."
  exit 1
fi

c_ylw "[4/6] observability backends"
# Order within this step: storage backends first, then the collector that
# writes to them, so the collector's exporters resolve on first start.
# shellcheck disable=SC2046
helm upgrade --install prometheus prometheus-community/prometheus \
  -n observability $(ver_flag "$CHART_VERSION_PROMETHEUS") \
  -f "$P/observability/values/prometheus.yaml" "${WAIT_FLAGS[@]}"

# shellcheck disable=SC2046
helm upgrade --install tempo grafana/tempo \
  -n observability $(ver_flag "$CHART_VERSION_TEMPO") \
  -f "$P/observability/values/tempo.yaml" "${WAIT_FLAGS[@]}"

# shellcheck disable=SC2046
helm upgrade --install loki grafana/loki \
  -n observability $(ver_flag "$CHART_VERSION_LOKI") \
  -f "$P/observability/values/loki.yaml" "${WAIT_FLAGS[@]}"

# shellcheck disable=SC2046
helm upgrade --install otel-collector open-telemetry/opentelemetry-collector \
  -n observability $(ver_flag "$CHART_VERSION_OTEL_COLLECTOR") \
  -f "$P/observability/values/otel-collector.yaml" "${WAIT_FLAGS[@]}"

# shellcheck disable=SC2046
helm upgrade --install grafana grafana/grafana \
  -n observability $(ver_flag "$CHART_VERSION_GRAFANA") \
  -f "$P/observability/values/grafana.yaml" "${WAIT_FLAGS[@]}"

c_ylw "[5/6] application (Astronomy Shop)"
# shellcheck disable=SC2046
helm upgrade --install astronomy open-telemetry/opentelemetry-demo \
  -n app $(ver_flag "$CHART_VERSION_OTEL_DEMO") \
  -f "$P/app/values/astronomy-shop.yaml" "${WAIT_FLAGS[@]}"

c_ylw "[6/6] chaos + rbac"
# shellcheck disable=SC2046
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  -n chaos-mesh $(ver_flag "$CHART_VERSION_CHAOS_MESH") \
  -f "$P/chaos/values/chaos-mesh.yaml" "${WAIT_FLAGS[@]}"

kubectl apply -f "$P/rbac/"

c_grn "Platform installed."
c_dim "Verify: make verify     UIs: make tunnel-grafana"
