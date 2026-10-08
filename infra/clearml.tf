# A reviewed AMI must contain ClearML Server and SSM Agent. The instance is
# reachable only through SSM; no public address or inbound security-group rules.
resource "aws_iam_role" "clearml" {

  count              = var.enable_clearml ? 1 : 0
  name               = "${local.prefix}-clearml"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })

}
resource "aws_iam_role_policy_attachment" "clearml_ssm" {

  count      = var.enable_clearml ? 1 : 0
  role       = aws_iam_role.clearml[0].name
  policy_arn = "${local.arn_prefix}:iam::aws:policy/AmazonSSMManagedInstanceCore"

}
resource "aws_iam_role_policy" "clearml_artifacts" {

  count = var.enable_clearml ? 1 : 0
  role  = aws_iam_role.clearml[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.app["data"].arn}/clearml/*" },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.app["data"].arn, Condition = { StringLike = { "s3:prefix" = "clearml/*" } } }
  ] })

}
resource "aws_iam_instance_profile" "clearml" {

  count = var.enable_clearml ? 1 : 0
  name  = "${local.prefix}-clearml"
  role  = aws_iam_role.clearml[0].name

}
resource "aws_security_group" "clearml" {

  count       = var.enable_clearml ? 1 : 0
  name        = "${local.prefix}-clearml"
  vpc_id      = var.clearml_vpc_id
  description = "No inbound rules; administrative access through SSM only"
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]

  }

}
resource "aws_instance" "clearml" {

  count                       = var.enable_clearml ? 1 : 0
  ami                         = var.clearml_ami_id
  instance_type               = var.clearml_instance_type
  subnet_id                   = var.clearml_subnet_id
  associate_public_ip_address = false
  iam_instance_profile        = aws_iam_instance_profile.clearml[0].name
  vpc_security_group_ids      = [aws_security_group.clearml[0].id]
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"

  }
  root_block_device {
    encrypted   = true
    volume_size = 100
    volume_type = "gp3"

  }
  lifecycle {

    precondition {

      condition     = var.clearml_vpc_id != "" && var.clearml_subnet_id != "" && var.clearml_ami_id != ""
      error_message = "ClearML requires a reviewed server AMI and a private subnet with SSM/S3 connectivity."

    }

  }
  tags = { Name = "${local.prefix}-clearml" }

}
