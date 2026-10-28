#!/usr/bin/env bash
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need k3d "Install k3d."
CLUSTER="${K3D_CLUSTER:-rca-bench}"
case "${1:-stop}" in
  stop)    k3d cluster stop "$CLUSTER";   c_grn "Stopped (state preserved)." ;;
  destroy) k3d cluster delete "$CLUSTER"; c_grn "Deleted." ;;
  *)       die "usage: local-down.sh {stop|destroy}" ;;
esac
