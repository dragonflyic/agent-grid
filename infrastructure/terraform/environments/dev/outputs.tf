output "coordinator_url" {
  description = "URL of the coordinator service"
  value       = "https://${module.apprunner.service_url}"
}

output "ecr_repository_url" {
  description = "ECR repository URL for pushing images"
  value       = module.apprunner.ecr_repository_url
}

output "database_endpoint" {
  description = "RDS database endpoint"
  value       = module.database.endpoint
}

output "github_webhook_url" {
  description = "GitHub webhook URL (for manual setup if needed)"
  value       = "https://${module.apprunner.service_url}/webhooks/github"
}

output "github_org" {
  description = "GitHub organization with webhook configured"
  value       = var.github_org
}

output "ecs_cluster_arn" {
  description = "ARN of the ECS cluster for scheduled tasks"
  value       = module.ecs_scheduled_task.cluster_arn
}

output "ecs_log_group_name" {
  description = "CloudWatch log group for coordinator scheduled tasks"
  value       = module.ecs_scheduled_task.log_group_name
}

output "schedule_rule_arn" {
  description = "EventBridge schedule rule ARN"
  value       = module.ecs_scheduled_task.schedule_rule_arn
}
