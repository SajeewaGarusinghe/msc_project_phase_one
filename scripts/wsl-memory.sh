#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Give WSL more of the machine's RAM by writing .wslconfig from inside WSL.
#
#   bash scripts/wsl-memory.sh [MEM] [CPUS]
#   bash scripts/wsl-memory.sh 24GB 8
#
# WSL defaults to about half of physical RAM. On a 32 GB laptop that is ~16 GB,
# and this bench reaches 95% of a 16 GB ceiling -- which is why Tempo was
# OOMKilled. Raising the allocation is the fix that costs nothing functionally.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
set +e

grep -qi microsoft /proc/version 2>/dev/null || die "Not running inside WSL."

MEM="${1:-24GB}"
CPUS="${2:-8}"

# Physical RAM, read from Windows rather than from WSL (which reports only its
# own allocation and would happily talk you into over-committing).
PHYS_BYTES="$(/mnt/c/Windows/System32/cmd.exe /c \
  "wmic computersystem get TotalPhysicalMemory /value" 2>/dev/null \
  | tr -d '\r' | grep -oE '[0-9]+' | head -1)"
if [[ -n "$PHYS_BYTES" ]]; then
  PHYS_GB=$(( PHYS_BYTES / 1024 / 1024 / 1024 ))
  c_dim "Physical RAM: ${PHYS_GB} GB"
  WANT_GB="${MEM%GB}"
  if (( WANT_GB > PHYS_GB - 6 )); then
    c_red "Requested ${MEM} of ${PHYS_GB} GB leaves Windows under 6 GB."
    c_red "Pick a smaller value: bash scripts/wsl-memory.sh $(( PHYS_GB - 8 ))GB"
    exit 1
  fi
fi

WINUSER="$(/mnt/c/Windows/System32/cmd.exe /c "echo %USERNAME%" 2>/dev/null | tr -d '\r\n')"
[[ -n "$WINUSER" ]] || die "Could not determine the Windows username."
CFG="/mnt/c/Users/${WINUSER}/.wslconfig"
c_dim "Writing $CFG"

if [[ -f "$CFG" ]]; then
  cp "$CFG" "${CFG}.bak" && c_dim "Existing config backed up to ${CFG}.bak"
fi

cat > "$CFG" <<CONF
[wsl2]
memory=${MEM}
processors=${CPUS}
swap=8GB
CONF

echo
c_grn "Written:"
sed 's/^/    /' "$CFG"
echo
c_ylw "This takes effect only after WSL fully restarts. From PowerShell:"
echo   "    wsl --shutdown"
echo   "then reopen Ubuntu, wait for Docker Desktop to come back, and run:"
echo   "    make bench TARGET=local"
