# ---------------------------------------------------------------------------
# Cost guardrails
#
# These exist because the realistic failure mode on a credit-funded account is
# not a wrong instance type -- it is leaving the bench running after a session.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "auto_stop" {
  count = var.auto_stop_enabled ? 1 : 0

  alarm_name          = "${var.project_name}-idle-auto-stop"
  alarm_description   = "Stop the bench node after sustained low CPU to protect credits."
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  comparison_operator = "LessThanThreshold"
  threshold           = var.auto_stop_cpu_threshold

  # CloudWatch basic monitoring emits every 5 minutes.
  period             = 300
  evaluation_periods = max(1, floor(var.auto_stop_idle_minutes / 5))

  # Without this, a stopped instance reports no data and the alarm sits in
  # INSUFFICIENT_DATA rather than re-arming cleanly.
  treat_missing_data = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.bench.id
  }

  alarm_actions = ["arn:aws:automate:${var.region}:ec2:stop"]
}

resource "aws_budgets_budget" "monthly" {
  count = var.budget_alert_email == "" ? 0 : 1

  name         = "${var.project_name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
