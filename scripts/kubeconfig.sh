#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Fetch the k3s kubeconfig from the bench node and rewrite it to point at the
# node's public address instead of 127.0.0.1.
#
# Uses SSM by default so no SSH key and no inbound port 22 are required.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [[ "$TARGET" == "local" ]]; then
  need k3d "Install k3d: https://k3d.io"
  k3d kubeconfig get "${K3D_CLUSTER:-rca-bench}" > "$KUBECONFIG_PATH"
  chmod 600 "$KUBECONFIG_PATH"
  c_grn "Local kubeconfig written to $KUBECONFIG_PATH"
  exit 0
fi

need aws "Install the AWS CLI v2."
IID="$(tf_out instance_id)"; REGION="$(tf_out region)"; IP="$(tf_out public_ip)"

c_ylw "Reading /etc/rancher/k3s/k3s.yaml from $IID over SSM ..."

# SSM send-command avoids needing an interactive session or an SSH key.
CMD_ID="$(aws ssm send-command \
  --instance-ids "$IID" --region "$REGION" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["cat /etc/rancher/k3s/k3s.yaml"]' \
  --query 'Command.CommandId' --output text)"

for _ in $(seq 1 30); do
  STATUS="$(aws ssm list-command-invocations --command-id "$CMD_ID" \
    --region "$REGION" --details \
    --query 'CommandInvocations[0].Status' --output text 2>/dev/null || echo Pending)"
  [[ "$STATUS" == "Success" || "$STATUS" == "Failed" ]] && break
  sleep 3
done
[[ "$STATUS" == "Success" ]] || die "SSM command did not succeed (status: $STATUS). Is the node running and the SSM agent up?"

aws ssm list-command-invocations --command-id "$CMD_ID" --region "$REGION" --details \
  --query 'CommandInvocations[0].CommandPlugins[0].Output' --output text \
  | sed "s#https://127.0.0.1:6443#https://${IP}:6443#" \
  > "$KUBECONFIG_PATH"

chmod 600 "$KUBECONFIG_PATH"
grep -q "server: https://${IP}:6443" "$KUBECONFIG_PATH" \
  || die "kubeconfig rewrite failed; inspect $KUBECONFIG_PATH"

c_grn "kubeconfig written to $KUBECONFIG_PATH"
c_dim "export KUBECONFIG=$KUBECONFIG_PATH"
