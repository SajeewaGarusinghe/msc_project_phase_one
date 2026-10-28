output "instance_id" {
  description = "Use with scripts/bench.sh start|stop|status."
  value       = aws_instance.bench.id
}

output "public_ip" {
  description = "Stable Elastic IP of the bench node."
  value       = aws_eip.bench.public_ip
}

output "region" {
  value = var.region
}

output "ssh_command" {
  description = "Direct SSH (only if enable_ssh = true and you supplied a key)."
  value       = var.ssh_public_key == "" ? "SSH key not configured - use SSM: aws ssm start-session --target ${aws_instance.bench.id} --region ${var.region}" : "ssh ubuntu@${aws_eip.bench.public_ip}"
}

output "ssm_command" {
  description = "Shell with no inbound port open. Recommended."
  value       = "aws ssm start-session --target ${aws_instance.bench.id} --region ${var.region}"
}

output "kubeconfig_hint" {
  description = "Fetch the kubeconfig once the node is up."
  value       = "make kubeconfig   # or: scripts/kubeconfig.sh"
}

output "estimated_monthly_usd_if_left_running" {
  description = <<-DESC
    Blunt reminder. This is compute only, at roughly on-demand us-east-1 rates,
    assuming 730 hours. Verify against the current AWS pricing page -- rates
    change and vary by region.
  DESC
  value       = "t3.xlarge ~ $120/mo, t3.2xlarge ~ $240/mo if never stopped. Stop it."
}
