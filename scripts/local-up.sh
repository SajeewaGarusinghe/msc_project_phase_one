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

# k3d returns once the containers are up; k3s inside them is not yet serving.
export KUBECONFIG="$KUBECONFIG_PATH"
if ! wait_for_api 90; then
  echo
  c_red "The API never became ready (180s). Diagnosing automatically:"

  echo
  c_ylw "=== k3d containers ==="
  docker ps -a --filter "name=k3d-${CLUSTER}" \
    --format '  {{.Names}}\t{{.Status}}' 2>/dev/null

  echo
  c_ylw "=== is the API port held by something else? ==="
  PORT="$(python3 -c "
import yaml
try: print(yaml.safe_load(open('$REPO_ROOT/infra/local/k3d-cluster.yaml'))['kubeAPI']['hostPort'])
except Exception: print('6445')
" 2>/dev/null || echo 6445)"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep ":${PORT}" | sed 's/^/  /' || echo "  nothing listening on ${PORT}"
  fi

  echo
  c_ylw "=== k3s server log (last 40 lines) ==="
  docker logs "k3d-${CLUSTER}-server-0" --tail 40 2>&1 | sed 's/^/  /'

  echo
  c_red "k3s is not serving. The usual cause after a 'wsl --shutdown' is that"
  c_red "the cluster's containers restarted into a state k3s cannot resume."
  echo
  c_ylw "Rebuild it (the platform reinstalls from scratch; nothing here is precious yet):"
  echo  "    make local-reset"
  echo  "    make bench TARGET=local"
  exit 1
fi

c_grn "Local bench up and the API is serving."
c_dim "Next: make install TARGET=local"
