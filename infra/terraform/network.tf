# ---------------------------------------------------------------------------
# Network
#
# COST NOTE: this VPC deliberately has NO NAT Gateway. A NAT Gateway is roughly
# $32/month plus data processing charges and is the single most common way a
# research account burns credits on infrastructure nobody is using. The node
# lives in a public subnet with a public IP and reaches the internet through the
# Internet Gateway, which is free.
# ---------------------------------------------------------------------------

resource "aws_vpc" "bench" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.project_name}-vpc" }
}

resource "aws_internet_gateway" "bench" {
  vpc_id = aws_vpc.bench.id
  tags   = { Name = "${var.project_name}-igw" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.bench.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project_name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.bench.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.bench.id
  }

  tags = { Name = "${var.project_name}-public-rt" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# Security group
#
# Everything the observability stack exposes (Grafana, Prometheus, Tempo, Loki)
# is reached by port-forward over SSM or SSH -- see scripts/tunnel.sh. Nothing
# but SSH and optionally the k3s API is ever opened to the network.
# ---------------------------------------------------------------------------

resource "aws_security_group" "bench" {
  name        = "${var.project_name}-sg"
  description = "Single-node k3s research bench"
  vpc_id      = aws_vpc.bench.id

  tags = { Name = "${var.project_name}-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  count = var.enable_ssh ? 1 : 0

  security_group_id = aws_security_group.bench.id
  description       = "SSH from the operator only"
  cidr_ipv4         = var.allowed_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "k8s_api" {
  count = var.expose_k8s_api ? 1 : 0

  security_group_id = aws_security_group.bench.id
  description       = "k3s API server from the operator only"
  cidr_ipv4         = var.allowed_cidr
  from_port         = 6443
  to_port           = 6443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.bench.id
  description       = "Outbound for image pulls, Helm charts and SSM"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
