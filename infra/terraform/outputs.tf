output "ecr_repository_url" {
  description = "Repository that CI pushes service images to."
  value       = aws_ecr_repository.services.repository_url
}

output "log_group_name" {
  description = "CloudWatch log group for all services."
  value       = aws_cloudwatch_log_group.services.name
}
