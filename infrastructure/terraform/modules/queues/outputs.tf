output "jobs_queue_url" {
  description = "URL of the jobs queue"
  value       = aws_sqs_queue.jobs.url
}

output "jobs_queue_arn" {
  description = "ARN of the jobs queue"
  value       = aws_sqs_queue.jobs.arn
}

output "results_queue_url" {
  description = "URL of the results queue"
  value       = aws_sqs_queue.results.url
}

output "results_queue_arn" {
  description = "ARN of the results queue"
  value       = aws_sqs_queue.results.arn
}

output "coordinator_policy_arn" {
  description = "ARN of the coordinator SQS policy"
  value       = aws_iam_policy.coordinator_sqs.arn
}

output "worker_policy_arn" {
  description = "ARN of the worker SQS policy"
  value       = aws_iam_policy.worker_sqs.arn
}

output "worker_access_key_id" {
  description = "Access key ID for local worker"
  value       = aws_iam_access_key.worker.id
}

output "worker_secret_access_key" {
  description = "Secret access key for local worker"
  value       = aws_iam_access_key.worker.secret
  sensitive   = true
}
