# ECS Fargate Scheduled Task for Agent Grid Coordinator
#
# Runs the coordinator's management loop on a schedule via EventBridge.
# The task scans issues, classifies, spawns Fly Machine workers, and monitors PRs.

# ECS Cluster (Fargate, no EC2 instances)
resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-scheduled"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = var.tags
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "coordinator" {
  name              = "/ecs/${var.project_name}-coordinator"
  retention_in_days = 14
  tags              = var.tags
}

# ─── IAM: ECS Task Execution Role (pulling images, writing logs, reading secrets)
resource "aws_iam_role" "ecs_execution" {
  name = "${var.project_name}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ecs_execution_base" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "secrets-access"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = var.secret_arns
    }]
  })
}

# ─── IAM: ECS Task Role (for the running container — currently no extra AWS permissions needed)
resource "aws_iam_role" "ecs_task" {
  name = "${var.project_name}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = var.tags
}

# ─── ECS Task Definition
resource "aws_ecs_task_definition" "coordinator" {
  family                   = "${var.project_name}-coordinator"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "coordinator"
    image     = var.ecr_image_uri
    essential = true

    command = ["./scripts/run-scheduled.sh"]

    environment = [
      for k, v in var.environment_variables : {
        name  = k
        value = v
      }
    ]

    secrets = [
      for k, v in var.environment_secrets : {
        name      = k
        valueFrom = v
      }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.coordinator.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "coordinator"
      }
    }
  }])

  tags = var.tags
}

# ─── EventBridge Rule (schedule)
resource "aws_cloudwatch_event_rule" "coordinator_schedule" {
  name                = "${var.project_name}-coordinator-schedule"
  description         = "Trigger coordinator cycle every ${var.schedule_minutes} minutes"
  schedule_expression = "rate(${var.schedule_minutes} minutes)"
  tags                = var.tags
}

# ─── IAM: EventBridge → ECS
resource "aws_iam_role" "eventbridge_ecs" {
  name = "${var.project_name}-eventbridge-ecs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "eventbridge_ecs" {
  name = "run-ecs-task"
  role = aws_iam_role.eventbridge_ecs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = [aws_ecs_task_definition.coordinator.arn]
        Condition = {
          ArnLike = {
            "ecs:cluster" = aws_ecs_cluster.main.arn
          }
        }
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.ecs_execution.arn,
          aws_iam_role.ecs_task.arn,
        ]
      }
    ]
  })
}

# ─── EventBridge Target → ECS Task
resource "aws_cloudwatch_event_target" "coordinator_ecs" {
  rule      = aws_cloudwatch_event_rule.coordinator_schedule.name
  target_id = "coordinator-ecs-task"
  arn       = aws_ecs_cluster.main.arn
  role_arn  = aws_iam_role.eventbridge_ecs.arn

  ecs_target {
    task_definition_arn = aws_ecs_task_definition.coordinator.arn
    task_count          = 1
    launch_type         = "FARGATE"

    network_configuration {
      subnets          = var.subnet_ids
      security_groups  = var.security_group_ids
      assign_public_ip = false
    }
  }
}
