variable "project_name" {
  description = "Project name for resource naming"
  type        = string
}

variable "visibility_timeout_seconds" {
  description = "Visibility timeout for job queue (should match execution timeout)"
  type        = number
  default     = 3600 # 1 hour
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
