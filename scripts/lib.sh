#!/usr/bin/env bash
# Shared helpers. Sourced by every script in this directory.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$REPO_ROOT/infra/terraform"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-$REPO_ROOT/kubeconfig}"

# Target selects where the cluster lives: "cloud" (EC2 + k3s) or "local" (k3d).
# Both run k3s, so everything in platform/ applies identically to either.
TARGET="${TARGET:-cloud}"

c_red()  { printf '\033[0;31m%s\033[0m\n' "$*"; }
c_grn()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
c_ylw()  { printf '\033[0;33m%s\033[0m\n' "$*"; }
c_dim()  { printf '\033[0;90m%s\033[0m\n' "$*"; }

die() { c_red "ERROR: $*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "'$1' not found on PATH. $2"
}

# Read a terraform output, failing with a useful message rather than a raw
# terraform stack trace when the stack has not been applied yet.
tf_out() {
  local key="$1"
  ( cd "$TF_DIR" && terraform output -raw "$key" 2>/dev/null ) \
    || die "terraform output '$key' unavailable. Has 'terraform apply' run in $TF_DIR?"
}

# Wait until the API server actually answers.
#
# `k3d cluster start` returns as soon as the CONTAINERS are running, which is
# well before k3s inside them is serving. Anything that runs immediately after
# gets "connection reset by peer" from an API that is merely not up yet -- a
# transient condition reported as a hard error.
wait_for_api() {
  local tries="${1:-90}" i
  for (( i = 1; i <= tries; i++ )); do
    if kubectl get --raw /readyz >/dev/null 2>&1; then
      [[ $i -gt 1 ]] && c_dim "  API ready after $(( i * 2 ))s"
      return 0
    fi
    [[ $i -eq 3 ]] && c_ylw "  waiting for the Kubernetes API ..."
    sleep 2
  done
  return 1
}

# Fail loudly when the API is unreachable. Every read-only tool here returns
# empty on an unreachable API, and empty reads as healthy -- so a check that
# does not assert reachability first will cheerfully report "no problems".
require_api() {
  wait_for_api "${1:-5}" && return 0
  c_red "Cannot reach the Kubernetes API."
  c_red "Nothing below can be trusted -- every query would return empty,"
  c_red "which looks identical to a healthy cluster."
  echo
  echo "  Is the cluster running?   k3d cluster list"
  echo "  Start it:                 make local-up"
  echo "  After 'wsl --shutdown' k3s needs ~30-60s before it serves."
  exit 1
}

require_kubeconfig() {
  [[ -f "$KUBECONFIG_PATH" ]] \
    || die "No kubeconfig at $KUBECONFIG_PATH. Run: make kubeconfig"
  export KUBECONFIG="$KUBECONFIG_PATH"
}
