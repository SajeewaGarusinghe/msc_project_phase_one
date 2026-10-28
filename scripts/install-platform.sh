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
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_kubeconfig
need helm "Install Helm 3: https://helm.sh/docs/intro/install/"
need kubectl "Install kubectl."

# shellcheck source=/dev/null
source "$REPO_ROOT/platform/versions.env"
P="$REPO_ROOT/platform"

# Turn an empty version into no flag at all, a pinned version into --version X.
ver_flag() { [[ -z "${1:-}" ]] && echo "" || echo "--version $1"; }

c_ylw "[1/5] namespaces"
kubectl apply -f "$P/namespaces.yaml"

c_ylw "[2/5] helm repositories"
helm repo add open-telemetry "$REPO_OTEL" >/dev/null 2>&1 || true
helm repo add prometheus-community "$REPO_PROMETHEUS" >/dev/null 2>&1 || true
helm repo add grafana "$REPO_GRAFANA" >/dev/null 2>&1 || true
helm repo add chaos-mesh "$REPO_CHAOS_MESH" >/dev/null 2>&1 || true
helm repo update >/dev/null

c_ylw "[3/5] observability backends"
# Order within this step: storage backends first, then the collector that
# writes to them, so the collector's exporters resolve on first start.
# shellcheck disable=SC2046
helm upgrade --install prometheus prometheus-community/prometheus \
  -n observability $(ver_flag "$CHART_VERSION_PROMETHEUS") \
  -f "$P/observability/values/prometheus.yaml" --wait --timeout 10m

# shellcheck disable=SC2046
helm upgrade --install tempo grafana/tempo \
  -n observability $(ver_flag "$CHART_VERSION_TEMPO") \
  -f "$P/observability/values/tempo.yaml" --wait --timeout 10m

# shellcheck disable=SC2046
helm upgrade --install loki grafana/loki \
  -n observability $(ver_flag "$CHART_VERSION_LOKI") \
  -f "$P/observability/values/loki.yaml" --wait --timeout 10m

# shellcheck disable=SC2046
helm upgrade --install otel-collector open-telemetry/opentelemetry-collector \
  -n observability $(ver_flag "$CHART_VERSION_OTEL_COLLECTOR") \
  -f "$P/observability/values/otel-collector.yaml" --wait --timeout 10m

# shellcheck disable=SC2046
helm upgrade --install grafana grafana/grafana \
  -n observability $(ver_flag "$CHART_VERSION_GRAFANA") \
  -f "$P/observability/values/grafana.yaml" --wait --timeout 10m

c_ylw "[4/5] application (Astronomy Shop)"
# shellcheck disable=SC2046
helm upgrade --install astronomy open-telemetry/opentelemetry-demo \
  -n app $(ver_flag "$CHART_VERSION_OTEL_DEMO") \
  -f "$P/app/values/astronomy-shop.yaml" --wait --timeout 20m

c_ylw "[5/5] chaos + rbac"
# shellcheck disable=SC2046
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  -n chaos-mesh $(ver_flag "$CHART_VERSION_CHAOS_MESH") \
  -f "$P/chaos/values/chaos-mesh.yaml" --wait --timeout 10m

kubectl apply -f "$P/rbac/"

c_grn "Platform installed."
c_dim "Verify: make verify     UIs: make tunnel-grafana"
