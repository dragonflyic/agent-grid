variable "project_name" {
  description = "Project name for resource naming"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "ecr_image_uri" {
  description = "Full ECR image URI (e.g., 123456.dkr.ecr.us-west-2.amazonaws.com/repo:latest)"
  type        = string
}

variable "cpu" {
  description = "Fargate task CPU units (256, 512, 1024, etc.)"
  type        = string
  default     = "512"
}

variable "memory" {
  description = "Fargate task memory in MiB"
  type        = string
  default     = "1024"
}

variable "schedule_minutes" {
  description = "How often to run the coordinator cycle (in minutes)"
  type        = number
  default     = 30
}

variable "subnet_ids" {
  description = "Private subnet IDs for the ECS task"
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security group IDs for the ECS task"
  type        = list(string)
}

variable "secret_arns" {
  description = "Secrets Manager ARNs that the execution role needs to read"
  type        = list(string)
  default     = []
}

variable "environment_variables" {
  description = "Plain-text environment variables for the container"
  type        = map(string)
  default     = {}
}

variable "environment_secrets" {
  description = "Secrets Manager references for sensitive env vars (name -> ARN:key::)"
  type        = map(string)
  default     = {}
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
