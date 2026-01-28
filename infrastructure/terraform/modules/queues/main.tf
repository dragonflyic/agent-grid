# SQS Queues for coordinator-worker communication

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.project_name}-jobs"
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  tags = var.tags
}

resource "aws_sqs_queue" "results" {
  name                       = "${var.project_name}-results"
  visibility_timeout_seconds = 300 # 5 minutes for result processing
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  tags = var.tags
}

# IAM policy for coordinator (can publish to jobs, consume from results)
resource "aws_iam_policy" "coordinator_sqs" {
  name        = "${var.project_name}-coordinator-sqs"
  description = "SQS access for coordinator service"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.jobs.arn
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.results.arn
      }
    ]
  })

  tags = var.tags
}

# IAM policy for local worker (can consume from jobs, publish to results)
resource "aws_iam_policy" "worker_sqs" {
  name        = "${var.project_name}-worker-sqs"
  description = "SQS access for local worker"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ChangeMessageVisibility"
        ]
        Resource = aws_sqs_queue.jobs.arn
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.results.arn
      }
    ]
  })

  tags = var.tags
}

# IAM user for local worker
resource "aws_iam_user" "worker" {
  name = "${var.project_name}-worker"
  tags = var.tags
}

resource "aws_iam_user_policy_attachment" "worker_sqs" {
  user       = aws_iam_user.worker.name
  policy_arn = aws_iam_policy.worker_sqs.arn
}

# Access keys for local worker
resource "aws_iam_access_key" "worker" {
  user = aws_iam_user.worker.name
}
