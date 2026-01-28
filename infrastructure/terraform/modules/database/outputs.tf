output "endpoint" {
  description = "Database endpoint"
  value       = aws_db_instance.main.endpoint
}

output "address" {
  description = "Database address (hostname)"
  value       = aws_db_instance.main.address
}

output "port" {
  description = "Database port"
  value       = aws_db_instance.main.port
}

output "database_name" {
  description = "Database name"
  value       = aws_db_instance.main.db_name
}

output "connection_string" {
  description = "PostgreSQL connection string"
  value       = "postgresql://${var.master_username}:${var.master_password}@${aws_db_instance.main.endpoint}/${aws_db_instance.main.db_name}"
  sensitive   = true
}

output "security_group_id" {
  description = "Security group ID for the database"
  value       = aws_security_group.db.id
}
