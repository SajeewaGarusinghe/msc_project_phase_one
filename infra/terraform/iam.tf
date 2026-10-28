# ---------------------------------------------------------------------------
# Instance IAM role
#
# Grants SSM Session Manager so you can get a shell with NO inbound port open
# and no SSH key to manage. This is the recommended access path; set
# enable_ssh = false once you have confirmed SSM works.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "bench" {
  name               = "${var.project_name}-instance-role"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.bench.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bench" {
  name = "${var.project_name}-instance-profile"
  role = aws_iam_role.bench.name
}
