variable "project_name" {
  description = "Project name for resource naming"
  type        = string
  default     = "agent-grid"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-west-2"
}

variable "database_password" {
  description = "Password for the database"
  type        = string
  sensitive   = true
}

variable "github_token" {
  description = "GitHub personal access token"
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_webhook_secret" {
  description = "GitHub webhook secret"
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_org" {
  description = "GitHub organization name for org-level webhook"
  type        = string
  default     = ""
}

variable "fly_api_token" {
  description = "Fly.io API token for spawning worker machines"
  type        = string
  sensitive   = true
  default     = ""
}

variable "anthropic_api_key" {
  description = "Anthropic API key for classification and planning"
  type        = string
  sensitive   = true
  default     = ""
}

variable "target_repo" {
  description = "GitHub repository to monitor (owner/repo)"
  type        = string
  default     = ""
}

variable "coordinator_url" {
  description = "Public URL of the coordinator API (for worker callbacks)"
  type        = string
  default     = ""
}

variable "fly_app_name" {
  description = "Fly.io app name for worker machines"
  type        = string
  default     = "agent-grid-workers"
}

variable "fly_worker_image" {
  description = "Docker image for Fly.io worker machines"
  type        = string
  default     = "registry.fly.io/agent-grid-workers:latest"
}

variable "dry_run" {
  description = "Enable dry-run mode (reads GitHub but logs writes instead of executing them)"
  type        = bool
  default     = false
}

variable "execution_backend" {
  description = "Execution backend: 'oz' (Warp Oz) or 'fly' (Fly Machines)"
  type        = string
  default     = "oz"
}

variable "warp_api_key" {
  description = "Warp Oz API key for cloud agent runs"
  type        = string
  sensitive   = true
  default     = ""
}

variable "oz_environment_id" {
  description = "Warp Oz environment ID (pre-configured with repo and tools)"
  type        = string
  default     = ""
}

