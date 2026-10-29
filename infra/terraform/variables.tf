# ---------------------------------------------------------------------------
# Core identity
# ---------------------------------------------------------------------------

variable "project_name" {
  description = "Prefix applied to every resource name and tag."
  type        = string
  default     = "msc-rca-bench"
}

variable "region" {
  description = "AWS region. Pick the cheapest one near you; us-east-1 is usually lowest."
  type        = string
  default     = "us-east-1"
}

# ---------------------------------------------------------------------------
# Instance sizing -- THE single biggest lever on credit burn
# ---------------------------------------------------------------------------

variable "instance_type" {
  description = <<-DESC
    Instance type for the k3s node.

    Sizing rationale (see docs/01-cost-control.md):
      - Astronomy Shop documents ~6 GB free RAM
      - Prometheus + Tempo + Loki + Collector  ~3-4 GB
      - k3s + system                           ~1-2 GB
      - Chaos Mesh                             ~0.5 GB

    t3.xlarge   (4 vCPU / 16 GB) -- default; workable, little headroom
    t3.2xlarge  (8 vCPU / 32 GB) -- comfortable, ~2x the hourly cost
    m7i-flex.xlarge (4 vCPU / 16 GB) -- often cheaper than t3.xlarge, check your region

    AWS Free Tier instances (t3.micro, 1 GB) CANNOT run this workload.
  DESC
  type        = string
  default     = "t3.xlarge"
}

variable "root_volume_gb" {
  description = <<-DESC
    Root EBS volume size in GB. Note this is billed even while the instance is
    STOPPED (~$0.08/GB-month for gp3), so it is the floor on your monthly cost.
    100 GB leaves room for container images plus early telemetry snapshots.
  DESC
  type        = number
  default     = 100
}

variable "use_spot" {
  description = <<-DESC
    Use a Spot instance (~60-70% cheaper) instead of On-Demand.

    Trade-off: Spot can be reclaimed with 2 minutes notice. That is tolerable for
    interactive development but NOT for the telemetry capture runs in P2, where an
    interruption mid-incident corrupts a snapshot. Leave false for capture work.
  DESC
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

variable "allowed_cidr" {
  description = <<-DESC
    CIDR allowed to reach SSH and the k3s API. Set this to YOUR public IP with a
    /32 mask, e.g. "203.0.113.4/32". Find it with: curl -s https://checkip.amazonaws.com

    Leaving this as 0.0.0.0/0 exposes the bench to the internet. Don't.
  DESC
  type        = string
  default     = "0.0.0.0/32" # deliberately unusable -- you must set this
}

variable "ssh_public_key" {
  description = <<-DESC
    Contents of your SSH public key (e.g. the text of ~/.ssh/id_ed25519.pub).
    Leave empty to rely solely on SSM Session Manager, which needs no open port
    and no key management. SSM is the recommended path.
  DESC
  type        = string
  default     = ""
}

variable "enable_ssh" {
  description = "Open port 22 to allowed_cidr. False means SSM-only access (recommended)."
  type        = bool
  default     = true
}

variable "expose_k8s_api" {
  description = <<-DESC
    Open port 6443 to allowed_cidr so kubectl works from your laptop directly.
    If false, reach the API by tunnelling over SSM/SSH instead (scripts/tunnel.sh).
  DESC
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Cost guardrails
# ---------------------------------------------------------------------------

variable "auto_stop_enabled" {
  description = <<-DESC
    Create a CloudWatch alarm that STOPS the instance after a period of very low
    CPU. This is the safety net for the classic failure mode: leaving the bench
    running over a weekend and losing a month of credits.
  DESC
  type        = bool
  default     = true
}

variable "auto_stop_idle_minutes" {
  description = "Minutes of sustained low CPU before the auto-stop alarm fires."
  type        = number
  default     = 60
}

variable "auto_stop_cpu_threshold" {
  description = "Average CPU percent below which the instance counts as idle."
  type        = number
  default     = 4
}

variable "budget_limit_usd" {
  description = <<-DESC
    Monthly budget in USD. An AWS Budgets alert emails you at 50/80/100 percent.
    Budgets themselves are free for the first two. Set budget_alert_email to arm this.
  DESC
  type        = number
  default     = 30
}

variable "budget_alert_email" {
  description = "Email for budget alerts. Empty disables the budget resource."
  type        = string
  default     = ""
}
