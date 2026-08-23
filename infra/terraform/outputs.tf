output "instance_id" {
  value = aws_instance.bot.id
}

output "public_ip" {
  value = aws_eip.bot.public_ip
}

output "ecr_repository_url" {
  value = aws_ecr_repository.polymaker.repository_url
}

output "region" {
  value = var.aws_region
}

output "ssm_parameter_prefix" {
  value = "/polymaker/"
}

output "cloudwatch_log_group" {
  value = aws_cloudwatch_log_group.polymaker.name
}
