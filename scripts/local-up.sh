#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Bring up the free local bench. Costs nothing; needs a machine with headroom.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need k3d "Install k3d: https://k3d.io/#installation"
need docker "Install Docker."

CLUSTER="${K3D_CLUSTER:-rca-bench}"

# Fail early and legibly on the most common cause of a broken local bench.
MEM_BYTES="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
MEM_GB=$(( MEM_BYTES / 1024 / 1024 / 1024 ))
if (( MEM_GB > 0 && MEM_GB < 12 )); then
  c_red "Docker reports only ${MEM_GB} GB of memory."
  c_red "The Astronomy Shop alone documents ~6 GB; with the observability"
  c_red "stack this bench needs 12 GB minimum, 20 GB to be comfortable."
  c_ylw "Raise Docker's memory limit, or use the cloud bench instead."
  exit 1
fi
c_dim "Docker memory: ${MEM_GB} GB"

if k3d cluster list 2>/dev/null | grep -q "^${CLUSTER}\b"; then
  c_ylw "Cluster '${CLUSTER}' already exists -- starting it."
  k3d cluster start "$CLUSTER"
else
  c_ylw "Creating cluster '${CLUSTER}' ..."
  k3d cluster create --config "$REPO_ROOT/infra/local/k3d-cluster.yaml"
fi

TARGET=local "$REPO_ROOT/scripts/kubeconfig.sh"
c_grn "Local bench up."
c_dim "Next: make install TARGET=local"
