output "database_secret_arn" {
  description = "ARN of the database secret"
  value       = aws_secretsmanager_secret.database.arn
}

output "github_secret_arn" {
  description = "ARN of the GitHub secret"
  value       = var.github_token != "" ? aws_secretsmanager_secret.github[0].arn : ""
}

output "coordinator_secret_arn" {
  description = "ARN of the coordinator secret (Fly + Anthropic)"
  value       = var.fly_api_token != "" || var.anthropic_api_key != "" ? aws_secretsmanager_secret.coordinator[0].arn : ""
}

output "all_secret_arns" {
  description = "List of all secret ARNs"
  value = compact([
    aws_secretsmanager_secret.database.arn,
    var.github_token != "" ? aws_secretsmanager_secret.github[0].arn : "",
    var.fly_api_token != "" || var.anthropic_api_key != "" ? aws_secretsmanager_secret.coordinator[0].arn : "",
  ])
}
