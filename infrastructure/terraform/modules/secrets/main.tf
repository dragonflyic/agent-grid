# Secrets Manager for Agent Grid

resource "aws_secretsmanager_secret" "database" {
  name        = "${var.project_name}/database"
  description = "Database credentials for Agent Grid"

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "database" {
  secret_id = aws_secretsmanager_secret.database.id
  secret_string = jsonencode({
    username          = var.database_username
    password          = var.database_password
    host              = var.database_host
    port              = var.database_port
    database          = var.database_name
    connection_string = "postgresql://${var.database_username}:${var.database_password}@${var.database_host}:${var.database_port}/${var.database_name}"
  })
}

resource "aws_secretsmanager_secret" "github" {
  count       = var.github_token != "" ? 1 : 0
  name        = "${var.project_name}/github"
  description = "GitHub credentials for Agent Grid"

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "github" {
  count     = var.github_token != "" ? 1 : 0
  secret_id = aws_secretsmanager_secret.github[0].id
  secret_string = jsonencode({
    token          = var.github_token
    webhook_secret = var.github_webhook_secret
  })
}
