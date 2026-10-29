#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-shot toolchain setup inside a WSL2 Ubuntu distro.
#
#   bash scripts/setup-wsl.sh
#
# Installs: build basics, k3d, kubectl, helm. Verifies Docker Desktop's WSL
# integration is actually reachable, which is the step people forget and the
# one that produces the most confusing downstream errors.
#
# Safe to re-run: every step is skipped if already satisfied.
# ---------------------------------------------------------------------------
set -euo pipefail

grn() { printf '\033[0;32m  [ OK ] %s\033[0m\n' "$*"; }
ylw() { printf '\033[0;33m  [ .. ] %s\033[0m\n' "$*"; }
red() { printf '\033[0;31m  [FAIL] %s\033[0m\n' "$*"; }
die() { red "$*"; exit 1; }

echo
echo "MSc RCA bench -- WSL toolchain setup"
echo "===================================="
echo

# --- 0. sanity -------------------------------------------------------------
grep -qi microsoft /proc/version 2>/dev/null \
  || die "This does not look like WSL. Run it inside your WSL2 Ubuntu shell."
grn "running inside WSL"

# Warn early about the filesystem-boundary cost rather than after a slow install.
case "$PWD" in
  /mnt/*) ylw "You are under $PWD -- a Windows drive."
          ylw "Helm installs and image pulls will be slow and chmod will not stick."
          ylw "Prefer working from your WSL home (~). Continuing anyway." ;;
esac

# --- 1. base packages ------------------------------------------------------
echo
echo "1. Base packages"
MISSING=()
for p in git make curl ca-certificates; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [ ${#MISSING[@]} -gt 0 ]; then
  ylw "installing: ${MISSING[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y -qq "${MISSING[@]}"
fi
grn "git, make, curl present"

# --- 2. Docker reachability ------------------------------------------------
# Docker Desktop must have WSL integration enabled FOR THIS DISTRO. Docker
# running on Windows is not the same as docker working inside Ubuntu, and the
# failure mode is confusing, so this section diagnoses rather than just failing.
echo
echo "2. Docker"

docker_diagnose() {
  echo
  ylw "diagnostics:"
  printf '    docker binary : %s\n' "$(command -v docker 2>/dev/null || echo 'not on PATH')"
  if command -v docker >/dev/null 2>&1; then
    printf '    resolves to   : %s\n' "$(readlink -f "$(command -v docker)" 2>/dev/null || echo '?')"
  fi
  printf '    DOCKER_HOST   : %s\n' "${DOCKER_HOST:-(unset)}"
  if [ -S /var/run/docker.sock ]; then
    printf '    docker.sock   : present (%s)\n' "$(stat -c '%A %U:%G' /var/run/docker.sock 2>/dev/null)"
  else
    printf '    docker.sock   : MISSING at /var/run/docker.sock\n'
  fi
  if [ -d /mnt/wsl/docker-desktop ]; then
    printf '    integration   : /mnt/wsl/docker-desktop present\n'
  else
    printf '    integration   : /mnt/wsl/docker-desktop ABSENT  <-- WSL integration is not active for this distro\n'
  fi
  printf '    groups        : %s\n' "$(id -nG)"
  echo
  ylw "actual error from 'docker info':"
  docker info 2>&1 | grep -Ei 'cannot connect|permission denied|refused|error' | head -3 | sed 's/^/      /'
  echo
}

if ! command -v docker >/dev/null 2>&1; then
  red "docker CLI not found in this distro."
  docker_diagnose
  red "Fix: Docker Desktop -> Settings -> Resources -> WSL Integration"
  red "     -> enable this distro -> Apply & Restart -> CLOSE AND REOPEN this shell."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  red "docker CLI present but the daemon is unreachable."
  docker_diagnose
  echo "  Most likely causes, in order:"
  echo
  echo "   1. WSL integration is not enabled for THIS distro."
  echo "      Docker Desktop -> Settings -> Resources -> WSL Integration"
  echo "      -> toggle on 'Ubuntu' -> Apply & Restart."
  echo
  echo "   2. It is enabled, but this shell predates it."
  echo "      The socket and PATH are wired in at shell start:"
  echo "        exit, then from PowerShell:  wsl --shutdown"
  echo "        reopen Ubuntu and re-run this script."
  echo
  echo "   3. Docker Desktop is not running on Windows. Start it and wait for"
  echo "      the whale icon to stop animating."
  echo
  echo "   4. Permission denied on the socket (see diagnostics above):"
  echo "        sudo usermod -aG docker \$USER   # then: wsl --shutdown"
  echo
  exit 1
fi
grn "docker daemon reachable"

# --- 3. k3d ----------------------------------------------------------------
echo
echo "3. k3d"
if command -v k3d >/dev/null 2>&1; then
  grn "k3d already installed ($(k3d version 2>/dev/null | head -1))"
else
  ylw "installing k3d"
  curl -sfL https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
  command -v k3d >/dev/null 2>&1 || die "k3d install failed"
  grn "k3d installed"
fi

# --- 4. kubectl ------------------------------------------------------------
echo
echo "4. kubectl"
if command -v kubectl >/dev/null 2>&1; then
  grn "kubectl already installed"
else
  ylw "installing kubectl"
  KV="$(curl -sfL https://dl.k8s.io/release/stable.txt)"
  [ -n "$KV" ] || die "could not resolve the stable kubectl version"
  TMP="$(mktemp -d)"
  curl -sfLo "$TMP/kubectl" "https://dl.k8s.io/release/${KV}/bin/linux/amd64/kubectl"
  # Verify against the published checksum rather than trusting the download.
  curl -sfLo "$TMP/kubectl.sha256" "https://dl.k8s.io/release/${KV}/bin/linux/amd64/kubectl.sha256"
  echo "$(cat "$TMP/kubectl.sha256")  $TMP/kubectl" | sha256sum -c - >/dev/null \
    || die "kubectl checksum mismatch -- not installing"
  sudo install -o root -g root -m 0755 "$TMP/kubectl" /usr/local/bin/kubectl
  rm -rf "$TMP"
  grn "kubectl ${KV} installed (checksum verified)"
fi

# --- 5. helm ---------------------------------------------------------------
echo
echo "5. helm"
if command -v helm >/dev/null 2>&1; then
  grn "helm already installed ($(helm version --short 2>/dev/null))"
else
  ylw "installing helm"
  curl -sfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
  command -v helm >/dev/null 2>&1 || die "helm install failed"
  grn "helm installed"
fi

# --- done ------------------------------------------------------------------
echo
echo "===================================="
grn "toolchain ready"
echo
echo "  docker  $(docker --version 2>/dev/null | cut -d, -f1)"
echo "  k3d     $(k3d version 2>/dev/null | head -1)"
echo "  kubectl $(kubectl version --client -o yaml 2>/dev/null | grep gitVersion | head -1 | awk '{print $2}')"
echo "  helm    $(helm version --short 2>/dev/null)"
echo
echo "Next:"
echo "  make local-up"
echo "  make install TARGET=local"
echo "  make verify  TARGET=local"
