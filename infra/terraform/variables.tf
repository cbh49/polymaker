variable "aws_region" {
  type        = string
  default     = "eu-west-1"
  description = "AWS region for the trading-bot instance."
}

variable "instance_type" {
  type        = string
  default     = "t3.medium"
  description = "EC2 size. t3.medium fits Playwright Chromium + the websocket monitor."
}

variable "key_name" {
  type        = string
  default     = ""
  description = "Optional EC2 key pair name for SSH. Leave empty to disable SSH."
}

variable "ssh_cidr" {
  type        = string
  default     = ""
  description = "CIDR allowed to SSH (port 22). Empty disables SSH ingress."
}

variable "image_uri" {
  type        = string
  default     = ""
  description = "ECR image URI to pull on boot (account.dkr.ecr.eu-west-1.amazonaws.com/polymaker:tag). Empty builds locally from /opt/polymaker."
}

variable "repo_url" {
  type        = string
  default     = ""
  description = "Git clone URL for this trading-bot repo (repo root is polymaker, not the breton monorepo)."
}

variable "git_ref" {
  type        = string
  default     = "main"
  description = "Git ref to check out on the instance."
}

variable "alert_email" {
  type        = string
  default     = ""
  description = "Optional SNS email for CloudWatch alarms. Empty skips the subscription."
}
