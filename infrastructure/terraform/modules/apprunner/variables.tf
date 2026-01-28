variable "project_name" {
  description = "Project name for resource naming"
  type        = string
}

variable "container_port" {
  description = "Port the container listens on"
  type        = number
  default     = 8000
}

variable "cpu" {
  description = "CPU units for the service (256, 512, 1024, 2048, 4096)"
  type        = string
  default     = "512"
}

variable "memory" {
  description = "Memory for the service (512, 1024, 2048, 3072, 4096, ...)"
  type        = string
  default     = "1024"
}

variable "environment_variables" {
  description = "Environment variables for the service"
  type        = map(string)
  default     = {}
}

variable "environment_secrets" {
  description = "Environment secrets (from Secrets Manager) for the service"
  type        = map(string)
  default     = {}
}

variable "sqs_policy_arn" {
  description = "ARN of the SQS policy to attach to the instance role"
  type        = string
  default     = ""
}

variable "attach_sqs_policy" {
  description = "Whether to attach the SQS policy"
  type        = bool
  default     = true
}

variable "secret_arns" {
  description = "ARNs of secrets the service needs access to"
  type        = list(string)
  default     = []
}

variable "attach_secrets_policy" {
  description = "Whether to attach the secrets policy"
  type        = bool
  default     = true
}

variable "vpc_connector_subnets" {
  description = "Subnet IDs for VPC connector (for RDS access)"
  type        = list(string)
  default     = null
}

variable "vpc_connector_security_groups" {
  description = "Security group IDs for VPC connector"
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
