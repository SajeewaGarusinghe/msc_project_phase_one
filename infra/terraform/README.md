# Terraform — P0 bench infrastructure

One EC2 instance running k3s. No EKS, no NAT Gateway, no load balancer: those
three are what turn a research account's credits into nothing.

## What it creates

| Resource | Why | Cost when stopped |
|---|---|---|
| VPC + public subnet + IGW | Network. No NAT Gateway by design (~$32/mo saved). | free |
| Security group | SSH + k3s API from your IP only. | free |
| IAM role + instance profile | SSM Session Manager, so no inbound port is strictly needed. | free |
| EC2 instance | The bench. | **$0** (stopped) |
| gp3 root volume | Persists across stop/start. | ~$0.08/GB-month |
| Elastic IP | Stable address across restarts. | small |
| CloudWatch alarm | Auto-stops the node when idle. | free tier |
| Budget (optional) | Emails you at 50/80/100% of limit. | free |

## Usage

```bash
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars          # set allowed_cidr at minimum

terraform init
terraform plan
terraform apply
```

Then from the repo root: `make kubeconfig && make install && make verify`.

## Before you apply

- `terraform validate` and `terraform plan` have **not** been run by the author —
  no AWS credentials or Terraform binary were available in the authoring
  environment. Review the plan output carefully on first apply.
- Confirm current pricing for your region. Rates quoted in comments are
  approximate us-east-1 on-demand figures and change over time.
- `allowed_cidr` defaults to an unusable value on purpose, so a careless apply
  cannot expose the bench to the internet.
