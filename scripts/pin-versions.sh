#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Rewrite platform/versions.env with the chart versions actually deployed.
#
# Run this once after a successful `make install`, then COMMIT the result.
# From then on the bench is reproducible and a chart upgrade is a reviewable
# diff rather than something that happens silently between two capture runs.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_kubeconfig
need helm "Install Helm 3."

v() { helm list -n "$2" -o json 2>/dev/null \
      | python3 -c "import sys,json;print(next((r['chart'].rsplit('-',1)[1] for r in json.load(sys.stdin) if r['name']=='$1'),''))"; }

F="$REPO_ROOT/platform/versions.env"
TMP="$F.tmp"
cp "$F" "$TMP"

set_v() { sed -i "s|^$1=.*|$1=\"$2\"|" "$TMP"; }

set_v CHART_VERSION_OTEL_DEMO      "$(v astronomy app)"
set_v CHART_VERSION_OTEL_COLLECTOR "$(v otel-collector observability)"
set_v CHART_VERSION_PROMETHEUS     "$(v prometheus observability)"
set_v CHART_VERSION_TEMPO          "$(v tempo observability)"
set_v CHART_VERSION_LOKI           "$(v loki observability)"
set_v CHART_VERSION_GRAFANA        "$(v grafana observability)"
set_v CHART_VERSION_CHAOS_MESH     "$(v chaos-mesh chaos-mesh)"

mv "$TMP" "$F"
c_grn "Pinned versions written to platform/versions.env"
c_dim "Review and commit:  git diff platform/versions.env"
grep '^CHART_VERSION' "$F"
