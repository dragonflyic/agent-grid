variable "project_name" {
  description = "Project name for resource naming"
  type        = string
}

variable "database_username" {
  description = "Database username"
  type        = string
}

variable "database_password" {
  description = "Database password"
  type        = string
  sensitive   = true
}

variable "database_host" {
  description = "Database host"
  type        = string
}

variable "database_port" {
  description = "Database port"
  type        = number
  default     = 5432
}

variable "database_name" {
  description = "Database name"
  type        = string
}

variable "github_app_id" {
  description = "GitHub App ID"
  type        = string
  default     = ""
}

variable "github_app_private_key" {
  description = "GitHub App private key (PEM-encoded)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_app_installation_id" {
  description = "GitHub App installation ID for the org"
  type        = string
  default     = ""
}

variable "github_webhook_secret" {
  description = "GitHub webhook secret"
  type        = string
  sensitive   = true
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

variable "warp_api_key" {
  description = "Warp Oz API key for cloud agent runs"
  type        = string
  sensitive   = true
  default     = ""
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
