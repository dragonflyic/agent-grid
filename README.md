# Agent Grid

Agent orchestration system for coding agents.

## Overview

Agent Grid is a system that orchestrates coding agents to work on GitHub issues. It consists of three main modules:

- **execution-grid**: Launches coding agents as subprocesses, publishes status to event bus
- **coordinator**: Central service deciding when/how to start agents, handles nudges
- **issue-tracker**: Issue tracking abstraction (filesystem or GitHub)

## Quick Start

1. Start PostgreSQL (schema is applied automatically):
   ```bash
   docker compose up -d
   ```

2. Run the server:
   ```bash
   poetry run agent-grid
   ```

3. Verify:
   ```bash
   curl localhost:8000/api/health
   ```

By default, Agent Grid uses the filesystem issue tracker storing issues in `./issues/`. No GitHub token is needed for local development.

To stop the database:
```bash
docker compose down
```

To stop and remove all data:
```bash
docker compose down -v
```

## API Endpoints

- `GET /` - Root endpoint
- `GET /api/health` - Health check
- `GET /api/executions` - List executions
- `GET /api/executions/{id}` - Get execution details
- `POST /api/nudge` - Create nudge request
- `GET /api/nudges` - List pending nudges
- `GET /api/budget` - Get budget status
- `POST /webhooks/github` - GitHub webhook endpoint

## Configuration

All configuration is done via environment variables with the `AGENT_GRID_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://postgres:dev@localhost:5432/agent_grid` | PostgreSQL connection URL |
| `ISSUE_TRACKER_TYPE` | `filesystem` | Issue tracker backend: `filesystem` or `github` |
| `ISSUES_DIRECTORY` | `./issues` | Directory for filesystem issue tracker |
| `GITHUB_TOKEN` | - | GitHub API token (required for github tracker) |
| `GITHUB_WEBHOOK_SECRET` | - | Secret for webhook signature verification |
| `MAX_CONCURRENT_EXECUTIONS` | `5` | Maximum concurrent agent executions |
| `EXECUTION_TIMEOUT_SECONDS` | `3600` | Execution timeout (1 hour) |
| `HOST` | `0.0.0.0` | Server host |
| `PORT` | `8000` | Server port |

## Issue Tracker

Agent Grid supports two issue tracker backends:

### Filesystem (default)

Issues are stored as markdown files with YAML frontmatter. This is ideal for local development and testing.

```bash
# Uses ./issues directory by default
export AGENT_GRID_ISSUE_TRACKER_TYPE=filesystem
export AGENT_GRID_ISSUES_DIRECTORY=./my-issues
```

Issue files are stored as `{id}.md` with this format:

```markdown
---
id: 1
title: "Issue title"
status: open
labels:
  - bug
parent_id: null
blocked_by:
  - 2
created_at: '2024-01-15T10:00:00+00:00'
updated_at: '2024-01-15T10:00:00+00:00'
---

Issue description goes here.

## Comments

### 2024-01-15T10:30:00+00:00
First comment text

### 2024-01-15T11:00:00+00:00
Second comment text
```

Features:
- Unique auto-incrementing IDs
- Title and body description
- Linear array of comments
- Subissues via `parent_id` field
- Blocking relationships via `blocked_by` field

### GitHub

For production use with GitHub Issues:

```bash
export AGENT_GRID_ISSUE_TRACKER_TYPE=github
export AGENT_GRID_GITHUB_TOKEN=your_token
```

## Development

```bash
# Install dependencies
poetry install

# Run tests
poetry run pytest

# Run server in development
poetry run python -m agent_grid.main
```
