#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need k3d "Install k3d."
CLUSTER="${K3D_CLUSTER:-rca-bench}"
case "${1:-stop}" in
  stop)    k3d cluster stop "$CLUSTER";   c_grn "Stopped (state preserved)." ;;
  destroy) k3d cluster delete "$CLUSTER"; c_grn "Deleted." ;;
  reset)
    # The recovery path when k3s will not resume. Everything here is
    # reproducible from the repo, so deleting is cheap -- but it IS a delete,
    # so it is a named action rather than something a start silently does.
    c_ylw "Deleting cluster '$CLUSTER' and recreating it ..."
    k3d cluster delete "$CLUSTER" 2>/dev/null || true
    "$REPO_ROOT/scripts/local-up.sh"
    ;;
  *)       die "usage: local-down.sh {stop|destroy|reset}" ;;
esac
