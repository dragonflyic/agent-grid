output "coordinator_url" {
  description = "URL of the coordinator service"
  value       = "https://${module.apprunner.service_url}"
}

output "ecr_repository_url" {
  description = "ECR repository URL for pushing images"
  value       = module.apprunner.ecr_repository_url
}

output "jobs_queue_url" {
  description = "SQS jobs queue URL (for local worker config)"
  value       = module.queues.jobs_queue_url
}

output "results_queue_url" {
  description = "SQS results queue URL (for local worker config)"
  value       = module.queues.results_queue_url
}

output "worker_access_key_id" {
  description = "AWS access key ID for local worker"
  value       = module.queues.worker_access_key_id
}

output "worker_secret_access_key" {
  description = "AWS secret access key for local worker"
  value       = module.queues.worker_secret_access_key
  sensitive   = true
}

output "database_endpoint" {
  description = "RDS database endpoint"
  value       = module.database.endpoint
}
