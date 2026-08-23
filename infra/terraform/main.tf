terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_ecr_repository" "polymaker" {
  name                 = "polymaker"
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_cloudwatch_log_group" "polymaker" {
  name              = "/polymaker/trading-bot"
  retention_in_days = 30
}

resource "aws_iam_role" "instance" {
  name = "polymaker-ec2"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cloudwatch" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy" "runtime" {
  name = "polymaker-runtime"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSecrets"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/polymaker/*"
      },
      {
        Sid      = "DecryptSsm"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = ["*"]
      },
      {
        Sid    = "EcrPull"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:DescribeLogStreams",
          "logs:DescribeLogGroups",
          "logs:PutLogEvents",
        ]
        Resource = [
          aws_cloudwatch_log_group.polymaker.arn,
          "${aws_cloudwatch_log_group.polymaker.arn}:*",
        ]
      },
    ]
  })
}

resource "aws_cloudwatch_log_stream" "monitor" {
  name           = "monitor"
  log_group_name = aws_cloudwatch_log_group.polymaker.name
}

resource "aws_cloudwatch_log_stream" "sharp" {
  name           = "sharp"
  log_group_name = aws_cloudwatch_log_group.polymaker.name
}

resource "aws_iam_instance_profile" "instance" {
  name = "polymaker-ec2"
  role = aws_iam_role.instance.name
}

resource "aws_security_group" "instance" {
  name        = "polymaker-ec2"
  description = "Polymaker trading bot - egress only by default"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  dynamic "ingress" {
    for_each = var.ssh_cidr != "" && var.key_name != "" ? [var.ssh_cidr] : []
    content {
      description = "SSH"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
    }
  }
}

resource "aws_instance" "bot" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  vpc_security_group_ids = [aws_security_group.instance.id]
  key_name               = var.key_name != "" ? var.key_name : null

  root_block_device {
    volume_size           = 40
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region    = var.aws_region
    image_uri = var.image_uri
    repo_url  = var.repo_url
    git_ref   = var.git_ref
    ecr_url   = aws_ecr_repository.polymaker.repository_url
  })

  tags = {
    Name = "polymaker-trading-bot"
    App  = "polymaker"
  }
}

resource "aws_eip" "bot" {
  instance = aws_instance.bot.id
  domain   = "vpc"

  tags = {
    Name = "polymaker-trading-bot"
  }
}

resource "aws_ssm_parameter" "placeholders" {
  for_each = toset([
    "POLY_PRIVATE_KEY",
    "POLY_FUNDER",
    "CONVEX_HTTP_URL",
    "CONVEX_PUBLISH_TOKEN",
    "POLYMAKER_LIVE",
  ])

  name        = "/polymaker/${each.key}"
  type        = "SecureString"
  value       = "CHANGE_ME"
  description = "Set the real value with: aws ssm put-parameter --name /polymaker/${each.key} --value '...' --overwrite --type SecureString --region ${var.aws_region}"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_sns_topic" "alerts" {
  count = var.alert_email != "" ? 1 : 0
  name  = "polymaker-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}
