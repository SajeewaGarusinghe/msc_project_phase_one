# ---------------------------------------------------------------------------
# The bench node
# ---------------------------------------------------------------------------

# Canonical publishes the current Ubuntu AMI per region as an SSM parameter, so
# no AMI ID is hardcoded and the config stays portable across regions.
data "aws_ssm_parameter" "ubuntu_2404" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

resource "aws_key_pair" "bench" {
  count = var.ssh_public_key == "" ? 0 : 1

  key_name   = "${var.project_name}-key"
  public_key = var.ssh_public_key
}

resource "aws_instance" "bench" {
  ami                    = data.aws_ssm_parameter.ubuntu_2404.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.bench.id]
  iam_instance_profile   = aws_iam_instance_profile.bench.name
  key_name               = var.ssh_public_key == "" ? null : aws_key_pair.bench[0].key_name

  # Stopping the instance is the primary cost control, so the root volume must
  # persist across stop/start. gp3 is cheaper and faster than gp2 at this size.
  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    delete_on_termination = true
    encrypted             = true
  }

  dynamic "instance_market_options" {
    for_each = var.use_spot ? [1] : []

    content {
      market_type = "spot"

      spot_options {
        # "stop" keeps the root volume on interruption so the bench can be
        # restarted rather than rebuilt. Requires a persistent request.
        instance_interruption_behavior = "stop"
        spot_instance_type             = "persistent"
      }
    }
  }

  user_data_replace_on_change = false
  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    project_name = var.project_name
  })

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  tags = {
    Name = "${var.project_name}-node"
    Role = "k3s-single-node"
  }

  lifecycle {
    # Lets you stop/start the instance by hand (or via the auto-stop alarm)
    # without Terraform fighting you on the next apply.
    ignore_changes = [ami]
  }
}

# An Elastic IP keeps the address stable across stop/start, which matters
# because allowed_cidr rules and kubeconfig both reference it.
# COST NOTE: an EIP attached to a RUNNING instance is free; AWS charges for
# EIPs that are allocated but not associated, and since 2024 charges for all
# public IPv4 addresses. The charge is a few dollars a month at most, and it
# saves regenerating the kubeconfig after every restart.
resource "aws_eip" "bench" {
  instance = aws_instance.bench.id
  domain   = "vpc"

  tags = { Name = "${var.project_name}-eip" }

  depends_on = [aws_internet_gateway.bench]
}
