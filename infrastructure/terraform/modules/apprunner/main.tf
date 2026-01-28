# App Runner for Agent Grid Coordinator

# ECR Repository for container images
resource "aws_ecr_repository" "main" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
}

# ECR Lifecycle policy to keep only recent images
resource "aws_ecr_lifecycle_policy" "main" {
  repository = aws_ecr_repository.main.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only 10 most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# IAM role for App Runner to access ECR
resource "aws_iam_role" "apprunner_ecr" {
  name = "${var.project_name}-apprunner-ecr"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "build.apprunner.amazonaws.com"
        }
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "apprunner_ecr" {
  role       = aws_iam_role.apprunner_ecr.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
}

# IAM role for App Runner instance (runtime)
resource "aws_iam_role" "apprunner_instance" {
  name = "${var.project_name}-apprunner-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "tasks.apprunner.amazonaws.com"
        }
      }
    ]
  })

  tags = var.tags
}

# Attach SQS policy to instance role
resource "aws_iam_role_policy_attachment" "apprunner_sqs" {
  count      = var.attach_sqs_policy ? 1 : 0
  role       = aws_iam_role.apprunner_instance.name
  policy_arn = var.sqs_policy_arn
}

# Attach Secrets Manager policy for database credentials
resource "aws_iam_role_policy" "apprunner_secrets" {
  count = var.attach_secrets_policy ? 1 : 0
  name  = "secrets-access"
  role  = aws_iam_role.apprunner_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.secret_arns
      }
    ]
  })
}

# VPC Connector for RDS access
resource "aws_apprunner_vpc_connector" "main" {
  count = var.vpc_connector_subnets != null ? 1 : 0

  vpc_connector_name = "${var.project_name}-${substr(md5(join(",", var.vpc_connector_subnets)), 0, 8)}"
  subnets            = var.vpc_connector_subnets
  security_groups    = var.vpc_connector_security_groups

  tags = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

# App Runner Service
resource "aws_apprunner_service" "main" {
  service_name = var.project_name

  source_configuration {
    auto_deployments_enabled = true

    authentication_configuration {
      access_role_arn = aws_iam_role.apprunner_ecr.arn
    }

    image_repository {
      image_identifier      = "${aws_ecr_repository.main.repository_url}:latest"
      image_repository_type = "ECR"

      image_configuration {
        port = tostring(var.container_port)

        runtime_environment_variables = merge(
          {
            AGENT_GRID_HOST            = "0.0.0.0"
            AGENT_GRID_PORT            = tostring(var.container_port)
            AGENT_GRID_DEPLOYMENT_MODE = "coordinator"
            PYTHONUNBUFFERED           = "1"
          },
          var.environment_variables
        )

        runtime_environment_secrets = var.environment_secrets
      }
    }
  }

  instance_configuration {
    cpu               = var.cpu
    memory            = var.memory
    instance_role_arn = aws_iam_role.apprunner_instance.arn
  }

  dynamic "network_configuration" {
    for_each = var.vpc_connector_subnets != null ? [1] : []
    content {
      egress_configuration {
        egress_type       = "VPC"
        vpc_connector_arn = aws_apprunner_vpc_connector.main[0].arn
      }
    }
  }

  health_check_configuration {
    healthy_threshold   = 1
    interval            = 5
    path                = "/"  # Use root endpoint which doesn't need DB
    protocol            = "HTTP"
    timeout             = 5
    unhealthy_threshold = 20  # Give even more time for startup
  }

  tags = var.tags
}
