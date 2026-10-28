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

require_kubeconfig() {
  [[ -f "$KUBECONFIG_PATH" ]] \
    || die "No kubeconfig at $KUBECONFIG_PATH. Run: make kubeconfig"
  export KUBECONFIG="$KUBECONFIG_PATH"
}
