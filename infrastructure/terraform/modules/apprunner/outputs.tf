output "service_url" {
  description = "URL of the App Runner service"
  value       = aws_apprunner_service.main.service_url
}

output "service_arn" {
  description = "ARN of the App Runner service"
  value       = aws_apprunner_service.main.arn
}

output "service_id" {
  description = "ID of the App Runner service"
  value       = aws_apprunner_service.main.service_id
}

output "ecr_repository_url" {
  description = "URL of the ECR repository"
  value       = aws_ecr_repository.main.repository_url
}

output "ecr_repository_arn" {
  description = "ARN of the ECR repository"
  value       = aws_ecr_repository.main.arn
}

output "instance_role_arn" {
  description = "ARN of the App Runner instance role"
  value       = aws_iam_role.apprunner_instance.arn
}
