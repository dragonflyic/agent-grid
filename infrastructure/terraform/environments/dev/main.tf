terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket = "agent-grid-terraform-state"
    key    = "dev/terraform.tfstate"
    region = "us-west-2"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "agent-grid"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}

provider "github" {
  token = var.github_token
  owner = var.github_org
}

# Data sources for existing VPC (use default VPC for MVP)
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Networking (NAT Gateway for App Runner to access external services)
module "networking" {
  source = "../../modules/networking"

  project_name     = var.project_name
  vpc_id           = data.aws_vpc.default.id
  vpc_cidr         = data.aws_vpc.default.cidr_block
  public_subnet_id = tolist(data.aws_subnets.default.ids)[0]

  tags = local.tags
}

# App Runner security group (for VPC connector to access RDS)
resource "aws_security_group" "apprunner" {
  name        = "${var.project_name}-apprunner"
  description = "Security group for App Runner VPC connector"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound traffic"
  }

  tags = local.tags
}

# New security group for private subnet VPC connector
resource "aws_security_group" "apprunner_private" {
  name        = "${var.project_name}-apprunner-private"
  description = "Security group for App Runner VPC connector in private subnets"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound traffic"
  }

  tags = local.tags
}

# Database
module "database" {
  source = "../../modules/database"

  project_name            = var.project_name
  vpc_id                  = data.aws_vpc.default.id
  subnet_ids              = data.aws_subnets.default.ids
  allowed_security_groups = [aws_security_group.apprunner.id, aws_security_group.apprunner_private.id]
  master_password         = var.database_password

  # Dev settings
  instance_class          = "db.t4g.micro"
  multi_az                = false
  deletion_protection     = false
  skip_final_snapshot     = true
  backup_retention_period = 1

  tags = local.tags
}

# Secrets
module "secrets" {
  source = "../../modules/secrets"

  project_name          = var.project_name
  database_username     = "agentgrid"
  database_password     = var.database_password
  database_host         = module.database.address
  database_port         = module.database.port
  database_name         = module.database.database_name
  github_token          = var.github_token
  github_webhook_secret = var.github_webhook_secret
  fly_api_token         = var.fly_api_token
  anthropic_api_key     = var.anthropic_api_key

  tags = local.tags
}

# App Runner
module "apprunner" {
  source = "../../modules/apprunner"

  project_name = var.project_name
  cpu          = "512"
  memory       = "1024"

  attach_sqs_policy     = false
  secret_arns           = module.secrets.all_secret_arns
  attach_secrets_policy = true

  vpc_connector_subnets         = module.networking.private_subnet_ids
  vpc_connector_security_groups = [aws_security_group.apprunner_private.id]

  environment_variables = {
    AGENT_GRID_AWS_REGION         = var.aws_region
    AGENT_GRID_ISSUE_TRACKER_TYPE = "github"
    AGENT_GRID_TARGET_REPO        = var.target_repo
    AGENT_GRID_FLY_APP_NAME       = var.fly_app_name
    AGENT_GRID_FLY_WORKER_IMAGE   = var.fly_worker_image
    AGENT_GRID_DRY_RUN            = var.dry_run ? "true" : "false"
    AGENT_GRID_COORDINATOR_URL    = var.coordinator_url
  }

  environment_secrets = merge(
    {
      AGENT_GRID_DATABASE_URL = "${module.secrets.database_secret_arn}:connection_string::"
    },
    var.github_token != "" ? {
      AGENT_GRID_GITHUB_TOKEN          = "${module.secrets.github_secret_arn}:token::"
      AGENT_GRID_GITHUB_WEBHOOK_SECRET = "${module.secrets.github_secret_arn}:webhook_secret::"
    } : {},
    module.secrets.coordinator_secret_arn != "" ? {
      AGENT_GRID_FLY_API_TOKEN    = "${module.secrets.coordinator_secret_arn}:fly_api_token::"
      AGENT_GRID_ANTHROPIC_API_KEY = "${module.secrets.coordinator_secret_arn}:anthropic_api_key::"
    } : {}
  )

  tags = local.tags
}

# ECS Scheduled Task (coordinator runs on schedule)
module "ecs_scheduled_task" {
  source = "../../modules/ecs-scheduled-task"

  project_name     = var.project_name
  aws_region       = var.aws_region
  ecr_image_uri    = "${module.apprunner.ecr_repository_url}:latest"
  cpu              = "512"
  memory           = "1024"
  schedule_minutes = 30

  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = [aws_security_group.apprunner_private.id]
  secret_arns        = module.secrets.all_secret_arns

  environment_variables = {
    AGENT_GRID_DEPLOYMENT_MODE    = "coordinator"
    AGENT_GRID_ISSUE_TRACKER_TYPE = "github"
    AGENT_GRID_TARGET_REPO        = var.target_repo
    AGENT_GRID_FLY_APP_NAME       = var.fly_app_name
    AGENT_GRID_FLY_WORKER_IMAGE   = var.fly_worker_image
    AGENT_GRID_DRY_RUN            = var.dry_run ? "true" : "false"
    PYTHONPATH                    = "/app/src"
    PYTHONUNBUFFERED               = "1"
  }

  environment_secrets = merge(
    {
      AGENT_GRID_DATABASE_URL = "${module.secrets.database_secret_arn}:connection_string::"
    },
    var.github_token != "" ? {
      AGENT_GRID_GITHUB_TOKEN = "${module.secrets.github_secret_arn}:token::"
    } : {},
    module.secrets.coordinator_secret_arn != "" ? {
      AGENT_GRID_FLY_API_TOKEN     = "${module.secrets.coordinator_secret_arn}:fly_api_token::"
      AGENT_GRID_ANTHROPIC_API_KEY = "${module.secrets.coordinator_secret_arn}:anthropic_api_key::"
    } : {}
  )

  tags = local.tags
}

locals {
  tags = {
    Project     = var.project_name
    Environment = "dev"
  }
}

# GitHub Organization Webhook
resource "github_organization_webhook" "agent_grid" {
  count = var.github_org != "" ? 1 : 0

  configuration {
    url          = "https://${module.apprunner.service_url}/webhooks/github"
    content_type = "json"
    secret       = var.github_webhook_secret
    insecure_ssl = false
  }

  events = ["issues", "issue_comment"]
  active = true
}
