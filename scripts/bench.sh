#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Start / stop / status for the cloud bench node.
#
# Stopping the node is THE cost control on this project: a stopped EC2 instance
# bills nothing for compute, only for the root volume (~$8/month at 100 GB).
# Get into the habit of running `make stop` when you finish a session.
# ---------------------------------------------------------------------------
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need aws "Install the AWS CLI v2: https://docs.aws.amazon.com/cli/"

ACTION="${1:-status}"
IID="$(tf_out instance_id)"
REGION="$(tf_out region)"

state() {
  aws ec2 describe-instances \
    --instance-ids "$IID" --region "$REGION" \
    --query 'Reservations[0].Instances[0].State.Name' --output text
}

case "$ACTION" in
  start)
    c_ylw "Starting $IID ..."
    aws ec2 start-instances --instance-ids "$IID" --region "$REGION" >/dev/null
    aws ec2 wait instance-running --instance-ids "$IID" --region "$REGION"
    c_grn "Running. k3s takes ~60s more to accept connections."
    c_dim "Next: make kubeconfig"
    ;;
  stop)
    c_ylw "Stopping $IID ..."
    aws ec2 stop-instances --instance-ids "$IID" --region "$REGION" >/dev/null
    aws ec2 wait instance-stopped --instance-ids "$IID" --region "$REGION"
    c_grn "Stopped. Compute billing has ceased; the root volume still bills."
    ;;
  status)
    S="$(state)"
    echo "instance : $IID"
    echo "region   : $REGION"
    echo "state    : $S"
    if [[ "$S" == "running" ]]; then
      c_ylw "Node is RUNNING and burning credits. 'make stop' when done."
    else
      c_grn "Node is $S. Only storage is billing."
    fi
    ;;
  *)
    die "usage: bench.sh {start|stop|status}"
    ;;
esac
