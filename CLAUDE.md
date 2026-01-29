# CLAUDE.md

This repo holds an application that optimistically tries to run coding agents against
GitHub issues.

## Key Locations

- `src/agent_grid/coordinator/`: API server that manages listening to GitHub issues and
  starting coding agent executions
- `src/agent_grid/execution_grid/`: Service that actually starts and manages the coding
  agent executions
- `infrastructure/terraform/`: Terraform configuration for production AWS deploy