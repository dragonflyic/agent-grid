terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # For MVP, use local state. Configure S3 backend for production:
  # backend "s3" {
  #   bucket = "agent-grid-terraform-state"
  #   key    = "dev/terraform.tfstate"
  #   region = "us-west-2"
  # }
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

# SQS Queues
module "queues" {
  source = "../../modules/queues"

  project_name               = var.project_name
  visibility_timeout_seconds = 3600 # 1 hour for long-running agents

  tags = local.tags
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

  project_name      = var.project_name
  database_username = "agentgrid"
  database_password = var.database_password
  database_host     = module.database.address
  database_port     = module.database.port
  database_name     = module.database.database_name
  github_token      = var.github_token
  github_webhook_secret = var.github_webhook_secret

  tags = local.tags
}

# App Runner
module "apprunner" {
  source = "../../modules/apprunner"

  project_name = var.project_name
  cpu          = "512"
  memory       = "1024"

  sqs_policy_arn        = module.queues.coordinator_policy_arn
  attach_sqs_policy     = true
  secret_arns           = module.secrets.all_secret_arns
  attach_secrets_policy = true

  vpc_connector_subnets         = module.networking.private_subnet_ids
  vpc_connector_security_groups = [aws_security_group.apprunner_private.id]

  environment_variables = {
    AGENT_GRID_AWS_REGION           = var.aws_region
    AGENT_GRID_SQS_JOB_QUEUE_URL    = module.queues.jobs_queue_url
    AGENT_GRID_SQS_RESULT_QUEUE_URL = module.queues.results_queue_url
    AGENT_GRID_ISSUE_TRACKER_TYPE   = "github"
  }

  environment_secrets = merge(
    {
      AGENT_GRID_DATABASE_URL = "${module.secrets.database_secret_arn}:connection_string::"
    },
    var.github_token != "" ? {
      AGENT_GRID_GITHUB_TOKEN        = "${module.secrets.github_secret_arn}:token::"
      AGENT_GRID_GITHUB_WEBHOOK_SECRET = "${module.secrets.github_secret_arn}:webhook_secret::"
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
