#!/usr/bin/env bash
# Create the two Unix users the A4 boundary needs (build guide A4 items 1-2). Idempotent.
#   provrca-sealer  owns the sealed-label store (mode 0700)
#   provrca-arm     runs every arm; cannot read the store, cannot list cases/ or a case dir
# Neither has a login shell or a home directory, and neither is in the other's group or in
# the runner's. CI runs this before the tests; on a workstation run it once (it uses sudo).
set -euo pipefail
for u in "${PROVRCA_SEALER_USER:-provrca-sealer}" "${PROVRCA_ARM_USER:-provrca-arm}"; do
  if getent passwd "$u" >/dev/null; then
    echo "user $u exists"
  else
    sudo useradd --system --no-create-home --user-group --shell /usr/sbin/nologin "$u"
    echo "created $u"
  fi
done
