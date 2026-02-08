# Tech Lead Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform the existing agent-grid from a webhook-triggered executor into a full Tech Lead Agent that scans all repo issues, classifies them, decomposes complex work, spawns sandboxed agents on Fly.io, and manages the PR review feedback loop.

**Architecture:** The orchestrator stays on AWS App Runner with PostgreSQL on RDS. SQS-based worker execution is replaced with ephemeral Fly Machines that boot, clone the target repo (inheriting its `.claude/skills/`), run Claude Code SDK, POST results back, and self-destruct. The management loop becomes a comprehensive 7-phase cron that scans GitHub, classifies, plans, assigns, and monitors.

**Tech Stack:** Python 3.12+, FastAPI, PostgreSQL (asyncpg), Claude Code SDK, Fly Machines API (httpx), Anthropic SDK (for classification/planning), GitHub API (httpx/Octokit-style)

---

## Task 1: Add Anthropic SDK dependency and repo config settings

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/agent_grid/config.py`

**Step 1: Add anthropic SDK to dependencies**

In `pyproject.toml`, add to `[tool.poetry.dependencies]`:
```
anthropic = "^0.52"
```

Remove the SQS/boto3 dependencies (we're replacing SQS with Fly):
```
# Remove these lines:
boto3 = "^1.35"
types-boto3 = {extras = ["sqs"], version = "^1.35"}
```

**Step 2: Add new config settings**

In `src/agent_grid/config.py`, add these fields to the `Settings` class:

```python
# Target repository
target_repo: str = ""  # e.g. "myorg/myrepo"

# Fly.io configuration
fly_api_token: str = ""
fly_app_name: str = ""  # Fly app name for worker machines
fly_worker_image: str = ""  # Docker image for workers e.g. "registry.fly.io/myapp/worker:latest"
fly_worker_cpus: int = 2
fly_worker_memory_mb: int = 2048
fly_worker_region: str = "iad"

# Anthropic API (for classification/planning - separate from Claude Code SDK)
anthropic_api_key: str = ""
classification_model: str = "claude-sonnet-4-5-20250929"
planning_model: str = "claude-sonnet-4-5-20250929"

# Management loop
management_loop_interval_seconds: int = 3600  # 1 hour

# Cost controls
max_tokens_per_run: int = 100000
max_cost_per_day_usd: float = 50.0
max_retries_per_issue: int = 2
```

Remove SQS-related settings:
```python
# Remove:
aws_region, sqs_job_queue_url, sqs_result_queue_url,
sqs_poll_interval_seconds, sqs_visibility_timeout_seconds
```

Change `deployment_mode` to remove "worker" option:
```python
deployment_mode: Literal["local", "coordinator"] = "local"
```

**Step 3: Run to verify**

Run: `cd /home/mohithg/fresa/agent-grid && poetry lock --no-update`
Expected: Lock file updated without errors

**Step 4: Commit**

```bash
git add pyproject.toml src/agent_grid/config.py
git commit -m "feat: add Fly.io, Anthropic SDK config; remove SQS settings"
```

---

## Task 2: Database schema — add issue_state and checkpoints

**Files:**
- Create: `src/agent_grid/migrations/versions/20260208_000000_add_issue_state_and_checkpoints.py`
- Modify: `src/agent_grid/coordinator/database.py`

**Step 1: Write the migration**

Create new Alembic migration at `src/agent_grid/migrations/versions/20260208_000000_add_issue_state_and_checkpoints.py`:

```python
"""Add issue_state table and checkpoint column to executions.

Revision ID: 002
Revises: 001
Create Date: 2026-02-08 00:00:00.000000+00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add checkpoint and mode columns to executions
    op.add_column("executions", sa.Column("checkpoint", sa.JSON(), nullable=True))
    op.add_column("executions", sa.Column("mode", sa.Text(), nullable=True, server_default="implement"))
    op.add_column("executions", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("executions", sa.Column("branch", sa.Text(), nullable=True))

    # Create issue_state table
    op.create_table(
        "issue_state",
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column("parent_issue", sa.Integer(), nullable=True),
        sa.Column("sub_issues", sa.ARRAY(sa.Integer()), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("issue_number", "repo"),
    )
    op.create_index("idx_issue_state_classification", "issue_state", ["classification"])
    op.create_index("idx_issue_state_repo", "issue_state", ["repo"])

    # Create cron_state table for tracking last-check timestamps
    op.create_table(
        "cron_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("cron_state")
    op.drop_index("idx_issue_state_repo", table_name="issue_state")
    op.drop_index("idx_issue_state_classification", table_name="issue_state")
    op.drop_table("issue_state")
    op.drop_column("executions", "branch")
    op.drop_column("executions", "pr_number")
    op.drop_column("executions", "mode")
    op.drop_column("executions", "checkpoint")
```

**Step 2: Add database methods for new tables**

Add these methods to `src/agent_grid/coordinator/database.py`:

```python
# Issue state operations

async def upsert_issue_state(
    self,
    issue_number: int,
    repo: str,
    classification: str | None = None,
    parent_issue: int | None = None,
    sub_issues: list[int] | None = None,
    retry_count: int = 0,
    metadata: dict | None = None,
) -> None:
    """Upsert an issue state record."""
    pool = await self._get_pool()
    await pool.execute(
        """
        INSERT INTO issue_state (issue_number, repo, classification, parent_issue, sub_issues, retry_count, metadata, last_checked_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())
        ON CONFLICT (issue_number, repo) DO UPDATE SET
            classification = COALESCE($3, issue_state.classification),
            parent_issue = COALESCE($4, issue_state.parent_issue),
            sub_issues = COALESCE($5, issue_state.sub_issues),
            retry_count = $6,
            metadata = COALESCE($7, issue_state.metadata),
            last_checked_at = NOW(),
            updated_at = NOW()
        """,
        issue_number, repo, classification, parent_issue, sub_issues, retry_count,
        json.dumps(metadata) if metadata else None,
    )

async def get_issue_state(self, issue_number: int, repo: str) -> dict | None:
    """Get issue state by number and repo."""
    pool = await self._get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM issue_state WHERE issue_number = $1 AND repo = $2",
        issue_number, repo,
    )
    return dict(row) if row else None

async def list_issue_states(self, repo: str, classification: str | None = None) -> list[dict]:
    """List issue states with optional classification filter."""
    pool = await self._get_pool()
    if classification:
        rows = await pool.fetch(
            "SELECT * FROM issue_state WHERE repo = $1 AND classification = $2",
            repo, classification,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM issue_state WHERE repo = $1",
            repo,
        )
    return [dict(row) for row in rows]

# Checkpoint operations

async def save_checkpoint(self, execution_id: UUID, checkpoint: dict) -> None:
    """Save a checkpoint for an execution."""
    pool = await self._get_pool()
    await pool.execute(
        "UPDATE executions SET checkpoint = $2 WHERE id = $1",
        execution_id, json.dumps(checkpoint),
    )

async def get_latest_checkpoint(self, issue_id: str) -> dict | None:
    """Get the most recent checkpoint for an issue."""
    pool = await self._get_pool()
    row = await pool.fetchrow(
        """
        SELECT checkpoint FROM executions
        WHERE issue_id = $1 AND checkpoint IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        issue_id,
    )
    if row and row["checkpoint"]:
        return json.loads(row["checkpoint"]) if isinstance(row["checkpoint"], str) else row["checkpoint"]
    return None

async def get_all_checkpoints(self, issue_id: str) -> list[dict]:
    """Get all checkpoints for an issue, newest first."""
    pool = await self._get_pool()
    rows = await pool.fetch(
        """
        SELECT id, checkpoint, mode, status, created_at, completed_at
        FROM executions
        WHERE issue_id = $1 AND checkpoint IS NOT NULL
        ORDER BY created_at DESC
        """,
        issue_id,
    )
    return [dict(row) for row in rows]

# Cron state operations

async def get_cron_state(self, key: str) -> dict | None:
    """Get a cron state value."""
    pool = await self._get_pool()
    row = await pool.fetchrow("SELECT value FROM cron_state WHERE key = $1", key)
    if row and row["value"]:
        return json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
    return None

async def set_cron_state(self, key: str, value: dict) -> None:
    """Set a cron state value."""
    pool = await self._get_pool()
    await pool.execute(
        """
        INSERT INTO cron_state (key, value, updated_at) VALUES ($1, $2, NOW())
        ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
        """,
        key, json.dumps(value),
    )

# Execution updates for new columns

async def update_execution_result(
    self,
    execution_id: UUID,
    status: ExecutionStatus,
    result: str | None = None,
    pr_number: int | None = None,
    branch: str | None = None,
    checkpoint: dict | None = None,
) -> None:
    """Update execution with result details."""
    pool = await self._get_pool()
    await pool.execute(
        """
        UPDATE executions
        SET status = $2, result = $3, pr_number = $4, branch = $5,
            checkpoint = $6, completed_at = NOW()
        WHERE id = $1
        """,
        execution_id, status.value, result, pr_number, branch,
        json.dumps(checkpoint) if checkpoint else None,
    )
```

Add `import json` at the top of `database.py`.

**Step 3: Commit**

```bash
git add src/agent_grid/migrations/versions/20260208_000000_add_issue_state_and_checkpoints.py src/agent_grid/coordinator/database.py
git commit -m "feat: add issue_state table, checkpoints, cron_state schema + DB methods"
```

---

## Task 3: Fly Machines client

**Files:**
- Create: `src/agent_grid/fly/__init__.py`
- Create: `src/agent_grid/fly/machines.py`

**Step 1: Create the Fly Machines API client**

Create `src/agent_grid/fly/__init__.py`:
```python
"""Fly.io integration for spawning ephemeral worker machines."""

from .machines import FlyMachinesClient, get_fly_client

__all__ = ["FlyMachinesClient", "get_fly_client"]
```

Create `src/agent_grid/fly/machines.py`:

```python
"""Fly Machines API client for spawning ephemeral worker containers."""

import logging
import time

import httpx

from ..config import settings

logger = logging.getLogger("agent_grid.fly")

FLY_API_BASE = "https://api.machines.dev/v1"


class FlyMachinesClient:
    """Client for the Fly Machines REST API.

    Spawns ephemeral Fly Machines that:
    1. Boot with the worker Docker image
    2. Clone the target repo
    3. Run Claude Code SDK against an issue
    4. POST results back to the orchestrator
    5. Self-destruct (auto_destroy=True)
    """

    def __init__(
        self,
        api_token: str | None = None,
        app_name: str | None = None,
    ):
        self._api_token = api_token or settings.fly_api_token
        self._app_name = app_name or settings.fly_app_name
        self._client = httpx.AsyncClient(
            base_url=FLY_API_BASE,
            headers={"Authorization": f"Bearer {self._api_token}"},
            timeout=60.0,
        )

    async def spawn_worker(
        self,
        execution_id: str,
        repo_url: str,
        issue_number: int,
        prompt: str,
        mode: str = "implement",
        context_json: str = "{}",
    ) -> dict:
        """Spawn an ephemeral Fly Machine for a worker agent.

        Args:
            execution_id: Unique execution ID.
            repo_url: Git repo URL to clone.
            issue_number: GitHub issue number.
            prompt: Full prompt for the agent.
            mode: Worker mode (implement, address_review, retry_with_feedback).
            context_json: JSON-encoded context (checkpoint, review comments, etc.).

        Returns:
            Fly Machine response dict with machine ID.
        """
        machine_name = f"worker-{issue_number}-{int(time.time())}"

        machine_config = {
            "name": machine_name,
            "config": {
                "image": settings.fly_worker_image,
                "env": {
                    "EXECUTION_ID": execution_id,
                    "REPO_URL": repo_url,
                    "ISSUE_NUMBER": str(issue_number),
                    "MODE": mode,
                    "PROMPT": prompt,
                    "CONTEXT_JSON": context_json,
                    "ANTHROPIC_API_KEY": settings.anthropic_api_key,
                    "GITHUB_TOKEN": settings.github_token,
                    "ORCHESTRATOR_URL": f"https://{self._app_name}.fly.dev",
                    "AGENT_BYPASS_PERMISSIONS": "true",
                },
                "guest": {
                    "cpu_kind": "shared",
                    "cpus": settings.fly_worker_cpus,
                    "memory_mb": settings.fly_worker_memory_mb,
                },
                "auto_destroy": True,
                "restart": {"policy": "no"},
            },
            "region": settings.fly_worker_region,
        }

        response = await self._client.post(
            f"/apps/{self._app_name}/machines",
            json=machine_config,
        )
        response.raise_for_status()

        machine = response.json()
        logger.info(
            f"Spawned Fly Machine {machine['id']} for issue #{issue_number} "
            f"(execution={execution_id}, mode={mode})"
        )
        return machine

    async def get_machine_status(self, machine_id: str) -> dict:
        """Get the status of a Fly Machine."""
        response = await self._client.get(
            f"/apps/{self._app_name}/machines/{machine_id}",
        )
        response.raise_for_status()
        return response.json()

    async def destroy_machine(self, machine_id: str) -> None:
        """Force destroy a Fly Machine."""
        try:
            await self._client.delete(
                f"/apps/{self._app_name}/machines/{machine_id}",
                params={"force": "true"},
            )
            logger.info(f"Destroyed Fly Machine {machine_id}")
        except httpx.HTTPStatusError as e:
            logger.warning(f"Failed to destroy machine {machine_id}: {e}")

    async def list_machines(self) -> list[dict]:
        """List all machines in the app."""
        response = await self._client.get(f"/apps/{self._app_name}/machines")
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


_fly_client: FlyMachinesClient | None = None


def get_fly_client() -> FlyMachinesClient:
    """Get the global Fly Machines client instance."""
    global _fly_client
    if _fly_client is None:
        _fly_client = FlyMachinesClient()
    return _fly_client
```

**Step 2: Commit**

```bash
git add src/agent_grid/fly/
git commit -m "feat: add Fly Machines API client for spawning ephemeral workers"
```

---

## Task 4: Fly-based ExecutionGrid implementation

**Files:**
- Create: `src/agent_grid/execution_grid/fly_grid.py`
- Modify: `src/agent_grid/execution_grid/service.py`
- Modify: `src/agent_grid/execution_grid/__init__.py`

**Step 1: Create FlyExecutionGrid**

Create `src/agent_grid/execution_grid/fly_grid.py`:

```python
"""Fly.io-based ExecutionGrid implementation for coordinator deployment.

Replaces SQS-based grid. Spawns ephemeral Fly Machines per execution.
Results come back via HTTP callback to /api/agent-status.
"""

import json
import logging
from typing import Callable, Awaitable
from uuid import UUID, uuid4

from .public_api import (
    AgentExecution,
    AgentEventHandler,
    Event,
    EventType,
    ExecutionConfig,
    ExecutionGrid,
    ExecutionStatus,
    utc_now,
)
from .event_bus import event_bus
from ..config import settings
from ..fly import get_fly_client

logger = logging.getLogger("agent_grid.fly_grid")


class FlyExecutionGrid(ExecutionGrid):
    """Fly Machines-based execution grid.

    - Spawns ephemeral Fly Machines for each execution
    - Machines POST results back to /api/agent-status
    - Orchestrator polls Fly API as fallback for stale machines
    """

    def __init__(self):
        self._fly = get_fly_client()
        self._executions: dict[UUID, AgentExecution] = {}
        self._machine_map: dict[UUID, str] = {}  # execution_id -> machine_id
        self._handler_mapping: dict[int, Callable[[Event], Awaitable[None]]] = {}

    async def launch_agent(
        self,
        config: ExecutionConfig,
        mode: str = "implement",
        issue_number: int | None = None,
        context: dict | None = None,
    ) -> UUID:
        """Launch an agent on an ephemeral Fly Machine."""
        execution_id = uuid4()

        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=config.prompt,
        )
        self._executions[execution_id] = execution

        try:
            machine = await self._fly.spawn_worker(
                execution_id=str(execution_id),
                repo_url=config.repo_url,
                issue_number=issue_number or 0,
                prompt=config.prompt,
                mode=mode,
                context_json=json.dumps(context or {}),
            )
            self._machine_map[execution_id] = machine["id"]

            await event_bus.publish(
                EventType.AGENT_STARTED,
                {
                    "execution_id": str(execution_id),
                    "repo_url": config.repo_url,
                    "machine_id": machine["id"],
                },
            )
            logger.info(f"Launched Fly Machine {machine['id']} for execution {execution_id}")

        except Exception as e:
            logger.error(f"Failed to spawn Fly Machine: {e}")
            execution.status = ExecutionStatus.FAILED
            execution.result = f"Failed to spawn worker: {e}"
            execution.completed_at = utc_now()
            self._executions.pop(execution_id, None)
            raise

        return execution_id

    async def handle_agent_result(
        self,
        execution_id: UUID,
        status: str,
        result: str | None = None,
        branch: str | None = None,
        pr_number: int | None = None,
        checkpoint: dict | None = None,
    ) -> None:
        """Handle a result callback from a Fly Machine worker.

        Called by the /api/agent-status endpoint.
        """
        execution = self._executions.get(execution_id)

        if status == "completed":
            if execution:
                execution.status = ExecutionStatus.COMPLETED
                execution.completed_at = utc_now()
                execution.result = result

            await event_bus.publish(
                EventType.AGENT_COMPLETED,
                {
                    "execution_id": str(execution_id),
                    "result": result,
                    "branch": branch,
                    "pr_number": pr_number,
                    "checkpoint": checkpoint,
                },
            )
            logger.info(f"Execution {execution_id} completed")
        else:
            if execution:
                execution.status = ExecutionStatus.FAILED
                execution.completed_at = utc_now()
                execution.result = result

            await event_bus.publish(
                EventType.AGENT_FAILED,
                {
                    "execution_id": str(execution_id),
                    "error": result,
                },
            )
            logger.info(f"Execution {execution_id} failed: {result}")

        self._executions.pop(execution_id, None)
        self._machine_map.pop(execution_id, None)

    async def get_execution_status(self, execution_id: UUID) -> AgentExecution | None:
        return self._executions.get(execution_id)

    def get_active_executions(self) -> list[AgentExecution]:
        return list(self._executions.values())

    async def cancel_execution(self, execution_id: UUID) -> bool:
        machine_id = self._machine_map.get(execution_id)
        if machine_id:
            await self._fly.destroy_machine(machine_id)
            execution = self._executions.get(execution_id)
            if execution:
                execution.status = ExecutionStatus.FAILED
                execution.completed_at = utc_now()
                execution.result = "Cancelled"
            await event_bus.publish(
                EventType.AGENT_FAILED,
                {"execution_id": str(execution_id), "error": "Cancelled"},
            )
            self._executions.pop(execution_id, None)
            self._machine_map.pop(execution_id, None)
            return True
        return False

    def subscribe_to_agent_events(self, handler: AgentEventHandler) -> None:
        async def event_handler(event: Event) -> None:
            await handler(event.type.value, event.payload)
        self._handler_mapping[id(handler)] = event_handler
        event_bus.subscribe(event_handler, event_type=None)

    def unsubscribe_from_agent_events(self, handler: AgentEventHandler) -> None:
        event_handler = self._handler_mapping.pop(id(handler), None)
        if event_handler:
            event_bus.unsubscribe(event_handler, event_type=None)


_fly_grid: FlyExecutionGrid | None = None


def get_fly_execution_grid() -> FlyExecutionGrid:
    global _fly_grid
    if _fly_grid is None:
        _fly_grid = FlyExecutionGrid()
    return _fly_grid
```

**Step 2: Update service.py to use Fly grid in coordinator mode**

In `src/agent_grid/execution_grid/service.py`, change the `get_execution_grid()` factory to return `FlyExecutionGrid` when `deployment_mode == "coordinator"` instead of `ExecutionGridClient` (SQS).

**Step 3: Update __init__.py exports**

Replace SQS exports with Fly exports in `src/agent_grid/execution_grid/__init__.py`.

**Step 4: Add agent-status callback endpoint**

Add to `src/agent_grid/coordinator/public_api.py`:

```python
from pydantic import BaseModel

class AgentStatusCallback(BaseModel):
    execution_id: str
    status: str  # "completed" or "failed"
    result: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    checkpoint: dict | None = None

@coordinator_router.post("/api/agent-status")
async def agent_status_callback(body: AgentStatusCallback):
    """Callback endpoint for Fly Machine workers to report results."""
    from ..execution_grid.fly_grid import get_fly_execution_grid
    grid = get_fly_execution_grid()
    await grid.handle_agent_result(
        execution_id=UUID(body.execution_id),
        status=body.status,
        result=body.result,
        branch=body.branch,
        pr_number=body.pr_number,
        checkpoint=body.checkpoint,
    )
    return {"status": "ok"}
```

**Step 5: Commit**

```bash
git add src/agent_grid/execution_grid/fly_grid.py src/agent_grid/execution_grid/service.py \
        src/agent_grid/execution_grid/__init__.py src/agent_grid/coordinator/public_api.py
git commit -m "feat: add Fly Machines-based ExecutionGrid, replace SQS grid"
```

---

## Task 5: GitHub label manager

**Files:**
- Create: `src/agent_grid/issue_tracker/label_manager.py`

**Step 1: Create label manager**

Create `src/agent_grid/issue_tracker/label_manager.py`:

```python
"""Label lifecycle management for the Tech Lead Agent.

Manages ai-* labels on GitHub issues to track pipeline state.
"""

import logging

from .github_client import GitHubClient
from .public_api import get_issue_tracker

logger = logging.getLogger("agent_grid.labels")

# All labels managed by the system
AI_LABELS = {
    "ai-in-progress",
    "ai-blocked",
    "ai-waiting",
    "ai-planning",
    "ai-review-pending",
    "ai-done",
    "ai-failed",
    "ai-skipped",
    "sub-issue",
    "epic",
}


class LabelManager:
    """Manages label transitions on GitHub issues."""

    def __init__(self):
        tracker = get_issue_tracker()
        if not isinstance(tracker, GitHubClient):
            raise TypeError("LabelManager requires GitHubClient")
        self._github = tracker

    async def transition_to(self, repo: str, issue_id: str, new_label: str) -> None:
        """Remove all ai-* labels and add the new one."""
        issue = await self._github.get_issue(repo, issue_id)
        current_ai_labels = [l for l in issue.labels if l in AI_LABELS]

        for label in current_ai_labels:
            if label != new_label:
                await self._github._remove_label(repo, issue_id, label)

        if new_label not in current_ai_labels:
            await self._github._add_label(repo, issue_id, new_label)

        logger.info(f"Issue #{issue_id}: transitioned to {new_label}")

    async def add_label(self, repo: str, issue_id: str, label: str) -> None:
        """Add a label without removing others."""
        await self._github._add_label(repo, issue_id, label)

    async def remove_label(self, repo: str, issue_id: str, label: str) -> None:
        """Remove a specific label."""
        await self._github._remove_label(repo, issue_id, label)

    async def ensure_labels_exist(self, repo: str) -> None:
        """Create all ai-* labels in the repo if they don't exist."""
        label_colors = {
            "ai-in-progress": "1d76db",
            "ai-blocked": "e4e669",
            "ai-waiting": "c5def5",
            "ai-planning": "d4c5f9",
            "ai-review-pending": "fbca04",
            "ai-done": "0e8a16",
            "ai-failed": "d93f0b",
            "ai-skipped": "cccccc",
            "sub-issue": "bfdadc",
            "epic": "3e4b9e",
        }
        for label, color in label_colors.items():
            try:
                await self._github._client.post(
                    f"/repos/{repo}/labels",
                    json={"name": label, "color": color},
                )
            except Exception:
                pass  # Label already exists


_label_manager: LabelManager | None = None


def get_label_manager() -> LabelManager:
    global _label_manager
    if _label_manager is None:
        _label_manager = LabelManager()
    return _label_manager
```

**Step 2: Commit**

```bash
git add src/agent_grid/issue_tracker/label_manager.py
git commit -m "feat: add label lifecycle manager for ai-* label transitions"
```

---

## Task 6: GitHub comment metadata parser

**Files:**
- Create: `src/agent_grid/issue_tracker/metadata.py`

**Step 1: Create metadata parser**

```python
"""Parse and embed structured metadata in GitHub issue comments.

Metadata is stored as hidden HTML comments:
<!-- TECH_LEAD_AGENT_META {"key": "value"} -->
"""

import json
import re

METADATA_PATTERN = re.compile(
    r"<!--\s*TECH_LEAD_AGENT_META\s*(\{.*?\})\s*-->",
    re.DOTALL,
)


def embed_metadata(comment_body: str, metadata: dict) -> str:
    """Append hidden metadata to a comment body."""
    meta_str = json.dumps(metadata, separators=(",", ":"))
    return f"{comment_body}\n\n<!-- TECH_LEAD_AGENT_META {meta_str} -->"


def extract_metadata(comment_body: str) -> dict | None:
    """Extract metadata from a comment body, if present."""
    match = METADATA_PATTERN.search(comment_body)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def strip_metadata(comment_body: str) -> str:
    """Remove metadata from a comment body."""
    return METADATA_PATTERN.sub("", comment_body).strip()
```

**Step 2: Commit**

```bash
git add src/agent_grid/issue_tracker/metadata.py
git commit -m "feat: add GitHub comment metadata embed/extract utilities"
```

---

## Task 7: Issue scanner (Phase 1)

**Files:**
- Create: `src/agent_grid/coordinator/scanner.py`

**Step 1: Create scanner**

```python
"""Phase 1: Scan GitHub for open issues to process.

Fetches all open issues from the target repo, filters out those
already being handled (via ai-* labels), and returns candidates.
"""

import logging

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.public_api import IssueInfo, IssueStatus

logger = logging.getLogger("agent_grid.scanner")

# Labels that indicate an issue is already being handled
HANDLED_LABELS = {
    "ai-in-progress",
    "ai-blocked",
    "ai-waiting",
    "ai-planning",
    "ai-review-pending",
    "ai-done",
    "ai-failed",
    "ai-skipped",
}


class Scanner:
    """Scans GitHub for unprocessed open issues."""

    def __init__(self):
        self._tracker = get_issue_tracker()

    async def scan(self, repo: str | None = None) -> list[IssueInfo]:
        """Scan for open issues that need processing.

        Returns issues that:
        - Are open
        - Have no ai-* labels (not already handled)
        - Are not pull requests

        Args:
            repo: Repository in owner/name format. Defaults to settings.target_repo.

        Returns:
            List of candidate issues.
        """
        repo = repo or settings.target_repo
        if not repo:
            logger.warning("No target_repo configured")
            return []

        all_open = await self._tracker.list_issues(repo, status=IssueStatus.OPEN)

        candidates = []
        for issue in all_open:
            # Skip if already has any ai-* label
            if any(label in HANDLED_LABELS for label in issue.labels):
                continue
            # Skip if assigned to a human (has assignees)
            # (GitHub API doesn't include assignees in our model yet, so skip this check)
            candidates.append(issue)

        logger.info(f"Scanned {repo}: {len(all_open)} open issues, {len(candidates)} candidates")
        return candidates


_scanner: Scanner | None = None


def get_scanner() -> Scanner:
    global _scanner
    if _scanner is None:
        _scanner = Scanner()
    return _scanner
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/scanner.py
git commit -m "feat: add issue scanner (Phase 1) — fetch unprocessed open issues"
```

---

## Task 8: Issue classifier (Phase 2)

**Files:**
- Create: `src/agent_grid/coordinator/classifier.py`

**Step 1: Create classifier**

```python
"""Phase 2: Classify issues using Claude API.

Calls Claude to classify each issue as SIMPLE, COMPLEX, BLOCKED, or SKIP.
"""

import json
import logging

import anthropic

from ..config import settings
from ..issue_tracker.public_api import IssueInfo

logger = logging.getLogger("agent_grid.classifier")


class Classification:
    """Result of classifying an issue."""

    def __init__(
        self,
        category: str,  # SIMPLE, COMPLEX, BLOCKED, SKIP
        reason: str,
        blocking_question: str | None = None,
        estimated_complexity: int = 5,
        dependencies: list[int] | None = None,
    ):
        self.category = category
        self.reason = reason
        self.blocking_question = blocking_question
        self.estimated_complexity = estimated_complexity
        self.dependencies = dependencies or []


CLASSIFICATION_PROMPT = """You are a senior tech lead. Given this GitHub issue, classify it.

Issue Title: {title}
Issue Body:
{body}

Labels: {labels}

Classify as ONE of:
A. SIMPLE — Can be done in a single PR by one agent. Estimated: < 200 lines changed, single concern, clear scope.
B. COMPLEX — Needs decomposition into sub-tasks. Estimated: multiple files/concerns, needs a plan first.
C. BLOCKED — Missing information, ambiguous requirements, needs human clarification before work can begin.
D. SKIP — Not suitable for AI (too creative, too risky, requires domain expertise beyond code).

Respond as JSON:
{{
  "category": "SIMPLE" | "COMPLEX" | "BLOCKED" | "SKIP",
  "reason": "one sentence explaining why",
  "blocking_question": "question for human, only if BLOCKED",
  "estimated_complexity": 1-10,
  "dependencies": [list of issue numbers this depends on, if any]
}}

Respond ONLY with the JSON object, no markdown fences."""


class Classifier:
    """Classifies GitHub issues using Claude API."""

    def __init__(self):
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def classify(self, issue: IssueInfo) -> Classification:
        """Classify a single issue.

        Args:
            issue: The issue to classify.

        Returns:
            Classification result.
        """
        prompt = CLASSIFICATION_PROMPT.format(
            title=issue.title,
            body=issue.body or "(no description)",
            labels=", ".join(issue.labels) if issue.labels else "(none)",
        )

        try:
            response = await self._client.messages.create(
                model=settings.classification_model,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            data = json.loads(text)

            classification = Classification(
                category=data["category"],
                reason=data.get("reason", ""),
                blocking_question=data.get("blocking_question"),
                estimated_complexity=data.get("estimated_complexity", 5),
                dependencies=data.get("dependencies", []),
            )
            logger.info(f"Issue #{issue.number}: classified as {classification.category} — {classification.reason}")
            return classification

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse classification for issue #{issue.number}: {e}")
            # Default to SIMPLE if parsing fails
            return Classification(category="SIMPLE", reason="Classification parse error, defaulting to SIMPLE")

        except Exception as e:
            logger.error(f"Classification API error for issue #{issue.number}: {e}")
            return Classification(category="SKIP", reason=f"Classification error: {e}")


_classifier: Classifier | None = None


def get_classifier() -> Classifier:
    global _classifier
    if _classifier is None:
        _classifier = Classifier()
    return _classifier
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/classifier.py
git commit -m "feat: add issue classifier (Phase 2) — SIMPLE/COMPLEX/BLOCKED/SKIP via Claude"
```

---

## Task 9: Task planner and decomposer (Phase 3 — COMPLEX path)

**Files:**
- Create: `src/agent_grid/coordinator/planner.py`

**Step 1: Create planner**

```python
"""Phase 3 (COMPLEX path): Decompose complex issues into sub-tasks.

Calls Claude to generate an implementation plan, then creates
sub-issues in GitHub with dependency tracking.
"""

import json
import logging

import anthropic

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.label_manager import get_label_manager
from ..issue_tracker.metadata import embed_metadata

logger = logging.getLogger("agent_grid.planner")


PLANNING_PROMPT = """You are a senior tech lead planning work decomposition.

Parent Issue #{issue_number}: {title}

{body}

Create an implementation plan:
1. Break this into the smallest possible independent sub-tasks
2. Each sub-task should be completable in a single PR (< 200 lines)
3. Identify dependencies between sub-tasks (what blocks what)
4. Order sub-tasks by dependency (leaves first)
5. Maximum {max_sub_issues} sub-tasks

Output as JSON:
{{
  "plan_summary": "Brief summary of the approach",
  "sub_tasks": [
    {{
      "title": "Short descriptive title",
      "description": "What to implement and why",
      "acceptance_criteria": ["Testable criterion 1", "Testable criterion 2"],
      "depends_on": [],
      "estimated_files": ["path/to/file.py"],
      "complexity": 1-5
    }}
  ],
  "risks": ["Potential risk 1"]
}}

Respond ONLY with the JSON object, no markdown fences."""


class Planner:
    """Decomposes complex issues into sub-tasks."""

    def __init__(self):
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._tracker = get_issue_tracker()
        self._labels = get_label_manager()

    async def decompose(self, repo: str, issue_number: int, title: str, body: str) -> list[dict]:
        """Decompose a complex issue into sub-issues.

        Args:
            repo: Repository in owner/name format.
            issue_number: Parent issue number.
            title: Parent issue title.
            body: Parent issue body.

        Returns:
            List of created sub-issue dicts with number and title.
        """
        # Label as planning
        await self._labels.transition_to(repo, str(issue_number), "ai-planning")

        prompt = PLANNING_PROMPT.format(
            issue_number=issue_number,
            title=title,
            body=body or "(no description)",
            max_sub_issues=10,
        )

        try:
            response = await self._client.messages.create(
                model=settings.planning_model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            plan = json.loads(text)
        except Exception as e:
            logger.error(f"Planning failed for issue #{issue_number}: {e}")
            await self._labels.transition_to(repo, str(issue_number), "ai-failed")
            await self._tracker.add_comment(
                repo, str(issue_number),
                f"Failed to create implementation plan: {e}",
            )
            return []

        # Create sub-issues
        created_issues = []
        for i, task in enumerate(plan.get("sub_tasks", [])):
            # Determine if this sub-issue has dependencies
            has_deps = bool(task.get("depends_on"))

            ac_list = "\n".join(f"- [ ] {ac}" for ac in task.get("acceptance_criteria", []))
            files_list = "\n".join(f"- `{f}`" for f in task.get("estimated_files", []))

            sub_body = f"""## Parent Issue
Part of #{issue_number}

## Task
{task.get('description', '')}

## Acceptance Criteria
{ac_list}

## Files Likely Affected
{files_list}

---
_Auto-generated by Tech Lead Agent from #{issue_number}_"""

            labels = ["sub-issue"]
            if has_deps:
                labels.append("ai-waiting")
            # No ai-* label for leaves — scanner will pick them up next cycle

            sub_issue = await self._tracker.create_subissue(
                repo=repo,
                parent_id=str(issue_number),
                title=f"[Sub #{issue_number}] {task['title']}",
                body=sub_body,
                labels=labels,
            )
            created_issues.append({"number": sub_issue.number, "title": task["title"]})
            logger.info(f"Created sub-issue #{sub_issue.number}: {task['title']}")

        # Update parent issue with plan summary
        plan_comment = f"""## Implementation Plan

{plan.get('plan_summary', '')}

### Sub-tasks
{chr(10).join(f"- #{ci['number']}: {ci['title']}" for ci in created_issues)}

### Risks
{chr(10).join(f"- {r}" for r in plan.get('risks', []))}"""

        plan_comment = embed_metadata(plan_comment, {
            "type": "plan",
            "parent_issue": issue_number,
            "sub_issues": [ci["number"] for ci in created_issues],
        })

        await self._tracker.add_comment(repo, str(issue_number), plan_comment)

        # Label parent as epic
        await self._labels.add_label(repo, str(issue_number), "epic")
        await self._labels.remove_label(repo, str(issue_number), "ai-planning")

        return created_issues


_planner: Planner | None = None


def get_planner() -> Planner:
    global _planner
    if _planner is None:
        _planner = Planner()
    return _planner
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/planner.py
git commit -m "feat: add planner (Phase 3) — decompose COMPLEX issues into sub-issues"
```

---

## Task 10: Prompt builder with modes

**Files:**
- Create: `src/agent_grid/coordinator/prompt_builder.py`

**Step 1: Create prompt builder**

This replaces the inline `_generate_prompt` in scheduler.py. It supports three modes: implement, address_review, retry_with_feedback.

```python
"""Build agent prompts for different execution modes.

Modes:
- implement: Fresh implementation of an issue
- address_review: Address PR review comments on existing branch
- retry_with_feedback: Retry after closed PR with human feedback
"""

from ..issue_tracker.public_api import IssueInfo


def build_prompt(
    issue: IssueInfo,
    repo: str,
    mode: str = "implement",
    context: dict | None = None,
    checkpoint: dict | None = None,
) -> str:
    """Build the full prompt for an agent execution.

    Args:
        issue: The issue to work on.
        repo: Repository in owner/name format.
        mode: Execution mode.
        context: Mode-specific context (review comments, feedback, etc.).
        checkpoint: Previous agent checkpoint for continuity.

    Returns:
        Full prompt string.
    """
    context = context or {}
    branch_name = f"agent/{issue.number}"

    base = f"""You are a senior software engineer working on a GitHub issue.

## Repository
- Repo: {repo}

## Your Task
Issue #{issue.number}: {issue.title}

{issue.body or '(no description)'}

## Rules
1. Work ONLY on what the issue asks for. Do not refactor unrelated code.
2. Write tests for your changes.
3. Run existing tests and make sure they pass.
4. Follow the existing code style in the repo.
5. Make atomic, well-described commits.
6. If you are BLOCKED and need human input:
   - Post a comment on the issue using: gh issue comment {issue.number} --repo {repo} --body "..."
   - Explain exactly what you need answered
   - Then EXIT
7. When done:
   - Push your branch
   - Create a PR using: gh pr create --title "..." --body "..."
   - Link the PR to the issue with "Closes #{issue.number}" in the body
   - After creating the PR, link it to the issue: gh pr edit --add-issue #{issue.number}
"""

    if mode == "implement":
        return base + f"""
## Setup
Create and checkout a working branch:
```bash
git checkout -b {branch_name}
```

After implementation:
```bash
git push -u origin {branch_name}
```
"""

    elif mode == "address_review":
        pr_number = context.get("pr_number")
        existing_branch = context.get("existing_branch", branch_name)
        review_comments = context.get("review_comments", "")

        prompt = base + f"""
## IMPORTANT: You are addressing review feedback on PR #{pr_number}

Previous work is already on branch: {existing_branch}
Checkout that branch (don't create a new one):
```bash
git checkout {existing_branch}
git pull origin {existing_branch}
```

Review comments to address:
{review_comments}

Address each comment. Push new commits to the same branch.
Do NOT force push. Do NOT squash. Add commits on top.
```bash
git push origin {existing_branch}
```
"""
        if checkpoint:
            prompt += f"""
## Previous Context
Here's what the previous agent run did, for your reference:
- Decisions made: {checkpoint.get('decisions_made', 'N/A')}
- Context: {checkpoint.get('context_summary', 'N/A')}
"""
        return prompt

    elif mode == "retry_with_feedback":
        closed_pr_number = context.get("closed_pr_number")
        human_feedback = context.get("human_feedback", "")
        what_not_to_do = context.get("what_not_to_do", "")
        new_branch = f"agent/{issue.number}-retry"

        prompt = base + f"""
## IMPORTANT: A previous attempt was made and the PR was closed.

Previous PR #{closed_pr_number} was closed by a human.
Here is what they said:
{human_feedback}

Here is what the previous attempt did (so you understand what NOT to repeat):
{what_not_to_do}

Take a DIFFERENT approach based on the feedback. Start fresh:
```bash
git checkout -b {new_branch}
```

After implementation:
```bash
git push -u origin {new_branch}
```
"""
        return prompt

    return base
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/prompt_builder.py
git commit -m "feat: add prompt builder with implement/address_review/retry_with_feedback modes"
```

---

## Task 11: PR monitor (Phase 5)

**Files:**
- Create: `src/agent_grid/coordinator/pr_monitor.py`

**Step 1: Create PR monitor**

```python
"""Phase 5: Monitor agent PRs for human review comments.

Checks open PRs created by agents. When a human leaves review comments,
spawns a new agent to address the feedback on the existing branch.
"""

import logging
from datetime import datetime

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.public_api import IssueInfo
from .database import get_database

logger = logging.getLogger("agent_grid.pr_monitor")


class PRMonitor:
    """Watches agent-created PRs for human review comments."""

    def __init__(self):
        self._tracker = get_issue_tracker()
        self._db = get_database()

    async def check_prs(self, repo: str) -> list[dict]:
        """Check all agent PRs for new review comments.

        Returns list of PRs that need review handling:
        [{"pr_number": N, "issue_id": "...", "review_comments": "...", "branch": "..."}]
        """
        from ..issue_tracker.github_client import GitHubClient
        if not isinstance(self._tracker, GitHubClient):
            return []

        github = self._tracker

        # Get last check time
        last_check_state = await self._db.get_cron_state("last_pr_check")
        last_check = last_check_state.get("timestamp") if last_check_state else None

        # Fetch open PRs with ai-review-pending label
        prs_needing_attention = []

        try:
            response = await github._client.get(
                f"/repos/{repo}/pulls",
                params={"state": "open", "per_page": 100},
            )
            response.raise_for_status()
            prs = response.json()
        except Exception as e:
            logger.error(f"Failed to fetch PRs: {e}")
            return []

        for pr in prs:
            # Only check PRs from agent branches
            head_branch = pr.get("head", {}).get("ref", "")
            if not head_branch.startswith("agent/"):
                continue

            pr_number = pr["number"]

            # Fetch review comments
            try:
                reviews_resp = await github._client.get(
                    f"/repos/{repo}/pulls/{pr_number}/reviews",
                    params={"per_page": 100},
                )
                reviews_resp.raise_for_status()
                reviews = reviews_resp.json()

                comments_resp = await github._client.get(
                    f"/repos/{repo}/pulls/{pr_number}/comments",
                    params={"per_page": 100},
                )
                comments_resp.raise_for_status()
                pr_comments = comments_resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch reviews for PR #{pr_number}: {e}")
                continue

            # Filter for new comments since last check
            new_reviews = []
            for review in reviews:
                if review.get("state") in ("CHANGES_REQUESTED", "COMMENTED") and review.get("body"):
                    if not last_check or review.get("submitted_at", "") > last_check:
                        new_reviews.append(review["body"])

            new_comments = []
            for comment in pr_comments:
                if not last_check or comment.get("created_at", "") > last_check:
                    path = comment.get("path", "")
                    body = comment.get("body", "")
                    new_comments.append(f"File: {path}\n{body}")

            if new_reviews or new_comments:
                all_feedback = "\n\n---\n\n".join(new_reviews + new_comments)

                # Extract linked issue number from PR body
                pr_body = pr.get("body", "") or ""
                issue_id = self._extract_issue_number(pr_body)

                prs_needing_attention.append({
                    "pr_number": pr_number,
                    "issue_id": issue_id,
                    "review_comments": all_feedback,
                    "branch": head_branch,
                })

        # Update last check timestamp
        await self._db.set_cron_state(
            "last_pr_check",
            {"timestamp": datetime.utcnow().isoformat()},
        )

        return prs_needing_attention

    async def check_closed_prs(self, repo: str) -> list[dict]:
        """Check recently closed (not merged) PRs for feedback (Phase 6).

        Returns list of closed PRs with human feedback.
        """
        from ..issue_tracker.github_client import GitHubClient
        if not isinstance(self._tracker, GitHubClient):
            return []

        github = self._tracker
        last_check_state = await self._db.get_cron_state("last_closed_pr_check")
        last_check = last_check_state.get("timestamp") if last_check_state else None

        prs_with_feedback = []

        try:
            response = await github._client.get(
                f"/repos/{repo}/pulls",
                params={"state": "closed", "per_page": 50, "sort": "updated", "direction": "desc"},
            )
            response.raise_for_status()
            prs = response.json()
        except Exception as e:
            logger.error(f"Failed to fetch closed PRs: {e}")
            return []

        for pr in prs:
            head_branch = pr.get("head", {}).get("ref", "")
            if not head_branch.startswith("agent/"):
                continue
            if pr.get("merged_at"):
                continue  # Skip merged PRs

            pr_number = pr["number"]
            closed_at = pr.get("closed_at", "")

            if last_check and closed_at <= last_check:
                continue

            # Get comments after close
            try:
                comments_resp = await github._client.get(
                    f"/repos/{repo}/issues/{pr_number}/comments",
                    params={"per_page": 50, "since": closed_at},
                )
                comments_resp.raise_for_status()
                comments = comments_resp.json()
            except Exception:
                continue

            feedback = [c["body"] for c in comments if c.get("body")]
            if not feedback:
                continue

            pr_body = pr.get("body", "") or ""
            issue_id = self._extract_issue_number(pr_body)

            prs_with_feedback.append({
                "pr_number": pr_number,
                "issue_id": issue_id,
                "human_feedback": "\n\n".join(feedback),
                "branch": head_branch,
            })

        await self._db.set_cron_state(
            "last_closed_pr_check",
            {"timestamp": datetime.utcnow().isoformat()},
        )

        return prs_with_feedback

    def _extract_issue_number(self, pr_body: str) -> str | None:
        """Extract linked issue number from PR body (Closes #N)."""
        import re
        match = re.search(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", pr_body, re.IGNORECASE)
        return match.group(1) if match else None


_pr_monitor: PRMonitor | None = None


def get_pr_monitor() -> PRMonitor:
    global _pr_monitor
    if _pr_monitor is None:
        _pr_monitor = PRMonitor()
    return _pr_monitor
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/pr_monitor.py
git commit -m "feat: add PR monitor (Phase 5+6) — detect review comments and closed PR feedback"
```

---

## Task 12: Blocker resolver (Phase 7)

**Files:**
- Create: `src/agent_grid/coordinator/blocker_resolver.py`

**Step 1: Create blocker resolver**

```python
"""Phase 7: Monitor ai-blocked issues for human responses.

When a human responds to a blocked issue, remove ai-blocked label
so the scanner picks it up again next cycle.
"""

import logging

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.public_api import IssueStatus
from ..issue_tracker.label_manager import get_label_manager
from .database import get_database

logger = logging.getLogger("agent_grid.blocker_resolver")


class BlockerResolver:
    """Resolves blocked issues when humans respond."""

    def __init__(self):
        self._tracker = get_issue_tracker()
        self._labels = get_label_manager()
        self._db = get_database()

    async def check_blocked_issues(self, repo: str) -> list[str]:
        """Check ai-blocked issues for new human comments.

        Returns list of issue IDs that were unblocked.
        """
        from ..issue_tracker.github_client import GitHubClient
        if not isinstance(self._tracker, GitHubClient):
            return []

        blocked_issues = await self._tracker.list_issues(
            repo, labels=["ai-blocked"],
        )

        last_check_state = await self._db.get_cron_state("last_blocker_check")
        last_check = last_check_state.get("timestamp") if last_check_state else None

        unblocked = []

        for issue in blocked_issues:
            # Check if there are new comments since last check
            has_new_comments = False
            for comment in issue.comments:
                comment_time = comment.created_at.isoformat() if comment.created_at else ""
                if not last_check or comment_time > last_check:
                    has_new_comments = True
                    break

            if has_new_comments:
                # Unblock: remove ai-blocked, scanner will pick it up
                await self._labels.remove_label(repo, issue.id, "ai-blocked")
                logger.info(f"Unblocked issue #{issue.number} — human responded")
                unblocked.append(issue.id)

        from datetime import datetime
        await self._db.set_cron_state(
            "last_blocker_check",
            {"timestamp": datetime.utcnow().isoformat()},
        )

        return unblocked


_blocker_resolver: BlockerResolver | None = None


def get_blocker_resolver() -> BlockerResolver:
    global _blocker_resolver
    if _blocker_resolver is None:
        _blocker_resolver = BlockerResolver()
    return _blocker_resolver
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/blocker_resolver.py
git commit -m "feat: add blocker resolver (Phase 7) — unblock issues on human response"
```

---

## Task 13: Dependency resolver

**Files:**
- Create: `src/agent_grid/coordinator/dependency_resolver.py`

**Step 1: Create dependency resolver**

```python
"""Sub-issue dependency tracking.

When a sub-issue is completed (PR merged, issue closed), check if
other sub-issues were waiting on it. If all dependencies are resolved,
remove ai-waiting label so scanner picks them up.

Also checks if all sub-issues of a parent are done, and closes the parent.
"""

import logging

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.public_api import IssueStatus
from ..issue_tracker.label_manager import get_label_manager

logger = logging.getLogger("agent_grid.dependency_resolver")


class DependencyResolver:
    """Resolves sub-issue dependencies and closes parent issues."""

    def __init__(self):
        self._tracker = get_issue_tracker()
        self._labels = get_label_manager()

    async def check_dependencies(self, repo: str) -> None:
        """Check all ai-waiting issues and unblock those with resolved deps."""
        waiting_issues = await self._tracker.list_issues(repo, labels=["ai-waiting"])

        for issue in waiting_issues:
            all_deps_resolved = True
            for blocker_id in issue.blocked_by:
                try:
                    blocker = await self._tracker.get_issue(repo, blocker_id)
                    if blocker.status != IssueStatus.CLOSED:
                        all_deps_resolved = False
                        break
                except Exception:
                    continue

            if all_deps_resolved:
                await self._labels.remove_label(repo, issue.id, "ai-waiting")
                logger.info(f"Unblocked sub-issue #{issue.number} — all dependencies resolved")

    async def check_parent_completion(self, repo: str) -> list[int]:
        """Check if any parent issues have all sub-issues completed.

        Returns list of parent issue numbers that were closed.
        """
        # Get all issues labeled "epic"
        epic_issues = await self._tracker.list_issues(repo, labels=["epic"])
        closed_parents = []

        for parent in epic_issues:
            if parent.status == IssueStatus.CLOSED:
                continue

            sub_issues = await self._tracker.list_subissues(repo, parent.id)
            if not sub_issues:
                continue

            all_done = all(sub.status == IssueStatus.CLOSED for sub in sub_issues)
            if all_done:
                await self._tracker.add_comment(
                    repo, parent.id,
                    "All sub-tasks completed! Closing parent issue.",
                )
                await self._tracker.update_issue_status(repo, parent.id, IssueStatus.CLOSED)
                await self._labels.transition_to(repo, parent.id, "ai-done")
                logger.info(f"Closed parent issue #{parent.number} — all sub-issues done")
                closed_parents.append(parent.number)

        return closed_parents


_resolver: DependencyResolver | None = None


def get_dependency_resolver() -> DependencyResolver:
    global _resolver
    if _resolver is None:
        _resolver = DependencyResolver()
    return _resolver
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/dependency_resolver.py
git commit -m "feat: add dependency resolver — unblock waiting sub-issues, close completed parents"
```

---

## Task 14: Rewrite management loop as 7-phase cron

**Files:**
- Modify: `src/agent_grid/coordinator/management_loop.py`

**Step 1: Rewrite management_loop.py**

Replace the existing skeletal implementation with the full 7-phase cron loop:

```python
"""The Tech Lead's main cron loop.

Runs every N seconds (default 1 hour). Each cycle performs 7 phases:
1. Scan — fetch unprocessed open issues
2. Classify — SIMPLE/COMPLEX/BLOCKED/SKIP
3. Act — spawn agents, create sub-issues, post questions
4. Monitor in-progress — check agent statuses
5. Monitor PRs — detect human review comments
6. Monitor closed PRs — detect feedback on closed PRs
7. Resolve blockers — unblock issues with human responses
"""

import asyncio
import logging

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.label_manager import get_label_manager
from ..issue_tracker.metadata import embed_metadata
from .database import get_database
from .scanner import get_scanner
from .classifier import get_classifier
from .planner import get_planner
from .prompt_builder import build_prompt
from .pr_monitor import get_pr_monitor
from .blocker_resolver import get_blocker_resolver
from .dependency_resolver import get_dependency_resolver
from .budget_manager import get_budget_manager

logger = logging.getLogger("agent_grid.cron")


class ManagementLoop:
    def __init__(self, interval_seconds: int | None = None):
        self._interval = interval_seconds or settings.management_loop_interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._db = get_database()
        self._tracker = get_issue_tracker()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Management loop started (interval={self._interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        # Run first cycle after a short delay (let other services start)
        await asyncio.sleep(10)
        while self._running:
            try:
                await self.run_cycle()
            except Exception:
                logger.exception("Error in management loop cycle")
            await asyncio.sleep(self._interval)

    async def run_cycle(self) -> None:
        """Run one full cycle of all 7 phases."""
        repo = settings.target_repo
        if not repo:
            logger.warning("No target_repo configured, skipping cycle")
            return

        logger.info(f"=== Starting cron cycle for {repo} ===")

        # Phase 1: Scan
        scanner = get_scanner()
        candidates = await scanner.scan(repo)
        logger.info(f"Phase 1: Found {len(candidates)} candidate issues")

        # Phase 2 + 3: Classify and act
        classifier = get_classifier()
        budget = get_budget_manager()
        planner = get_planner()
        labels = get_label_manager()

        for issue in candidates:
            can_launch, reason = await budget.can_launch_agent()
            if not can_launch:
                logger.info(f"Budget limit reached: {reason}. Stopping new assignments.")
                break

            classification = await classifier.classify(issue)

            # Save classification to DB
            await self._db.upsert_issue_state(
                issue_number=issue.number,
                repo=repo,
                classification=classification.category,
            )

            if classification.category == "SIMPLE":
                await self._launch_simple(repo, issue)
            elif classification.category == "COMPLEX":
                await planner.decompose(repo, issue.number, issue.title, issue.body or "")
            elif classification.category == "BLOCKED":
                await labels.transition_to(repo, issue.id, "ai-blocked")
                question = classification.blocking_question or classification.reason
                comment = embed_metadata(
                    f"**Agent needs clarification:**\n\n{question}",
                    {"type": "blocked", "reason": classification.reason},
                )
                await self._tracker.add_comment(repo, issue.id, comment)
                logger.info(f"Issue #{issue.number}: BLOCKED — posted question")
            elif classification.category == "SKIP":
                await labels.transition_to(repo, issue.id, "ai-skipped")
                await self._tracker.add_comment(
                    repo, issue.id,
                    f"Skipping automated work: {classification.reason}",
                )
                logger.info(f"Issue #{issue.number}: SKIPPED — {classification.reason}")

        # Phase 4: Monitor in-progress
        await self._check_in_progress(repo)

        # Phase 5: Monitor PRs for review comments
        pr_monitor = get_pr_monitor()
        prs_needing_work = await pr_monitor.check_prs(repo)
        for pr_info in prs_needing_work:
            if pr_info["issue_id"]:
                await self._launch_review_handler(repo, pr_info)

        # Phase 6: Monitor closed PRs with feedback
        closed_prs = await pr_monitor.check_closed_prs(repo)
        for pr_info in closed_prs:
            if pr_info["issue_id"]:
                await self._launch_retry(repo, pr_info)

        # Phase 7: Resolve blockers
        blocker_resolver = get_blocker_resolver()
        unblocked = await blocker_resolver.check_blocked_issues(repo)
        if unblocked:
            logger.info(f"Phase 7: Unblocked {len(unblocked)} issues")

        # Bonus: Check dependency resolution
        dep_resolver = get_dependency_resolver()
        await dep_resolver.check_dependencies(repo)
        await dep_resolver.check_parent_completion(repo)

        logger.info("=== Cron cycle complete ===")

    async def _launch_simple(self, repo: str, issue) -> None:
        """Launch an agent for a SIMPLE issue."""
        from ..execution_grid import get_execution_grid, ExecutionConfig

        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ai-in-progress")

        prompt = build_prompt(issue, repo, mode="implement")
        config = ExecutionConfig(
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
        )

        grid = get_execution_grid()
        # Use the extended launch_agent if Fly grid (supports mode/issue_number)
        if hasattr(grid, 'launch_agent') and 'mode' in grid.launch_agent.__code__.co_varnames:
            execution_id = await grid.launch_agent(
                config, mode="implement", issue_number=issue.number,
            )
        else:
            execution_id = await grid.launch_agent(config)

        # Record in DB
        from ..execution_grid import AgentExecution, ExecutionStatus
        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=prompt,
        )
        await self._db.create_execution(execution, issue_id=issue.id)
        logger.info(f"Issue #{issue.number}: SIMPLE — launched agent {execution_id}")

    async def _launch_review_handler(self, repo: str, pr_info: dict) -> None:
        """Launch an agent to address PR review comments."""
        from ..execution_grid import get_execution_grid, ExecutionConfig

        issue_id = pr_info["issue_id"]
        issue = await self._tracker.get_issue(repo, issue_id)

        checkpoint = await self._db.get_latest_checkpoint(issue_id)

        context = {
            "pr_number": pr_info["pr_number"],
            "existing_branch": pr_info["branch"],
            "review_comments": pr_info["review_comments"],
        }

        prompt = build_prompt(issue, repo, mode="address_review", context=context, checkpoint=checkpoint)
        config = ExecutionConfig(
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
        )

        grid = get_execution_grid()
        if hasattr(grid, 'launch_agent') and 'mode' in grid.launch_agent.__code__.co_varnames:
            execution_id = await grid.launch_agent(
                config, mode="address_review", issue_number=int(issue_id),
                context=context,
            )
        else:
            execution_id = await grid.launch_agent(config)

        from ..execution_grid import AgentExecution, ExecutionStatus
        execution = AgentExecution(id=execution_id, repo_url=config.repo_url, prompt=prompt)
        await self._db.create_execution(execution, issue_id=issue_id)
        logger.info(f"PR #{pr_info['pr_number']}: launched review handler agent {execution_id}")

    async def _launch_retry(self, repo: str, pr_info: dict) -> None:
        """Launch a retry agent for a closed PR with feedback."""
        from ..execution_grid import get_execution_grid, ExecutionConfig

        issue_id = pr_info["issue_id"]
        issue = await self._tracker.get_issue(repo, issue_id)

        checkpoint = await self._db.get_latest_checkpoint(issue_id)

        # Check retry count
        issue_state = await self._db.get_issue_state(int(issue_id), repo)
        retry_count = (issue_state or {}).get("retry_count", 0)
        if retry_count >= settings.max_retries_per_issue:
            labels = get_label_manager()
            await labels.transition_to(repo, issue_id, "ai-failed")
            await self._tracker.add_comment(
                repo, issue_id,
                f"Max retries ({settings.max_retries_per_issue}) reached. Needs human intervention.",
            )
            return

        context = {
            "closed_pr_number": pr_info["pr_number"],
            "human_feedback": pr_info["human_feedback"],
            "what_not_to_do": checkpoint.get("context_summary", "") if checkpoint else "",
        }

        prompt = build_prompt(issue, repo, mode="retry_with_feedback", context=context, checkpoint=checkpoint)
        config = ExecutionConfig(repo_url=f"https://github.com/{repo}.git", prompt=prompt)

        grid = get_execution_grid()
        if hasattr(grid, 'launch_agent') and 'mode' in grid.launch_agent.__code__.co_varnames:
            execution_id = await grid.launch_agent(
                config, mode="retry_with_feedback", issue_number=int(issue_id),
                context=context,
            )
        else:
            execution_id = await grid.launch_agent(config)

        # Increment retry count
        await self._db.upsert_issue_state(
            issue_number=int(issue_id), repo=repo,
            retry_count=retry_count + 1,
        )

        labels = get_label_manager()
        await labels.transition_to(repo, issue_id, "ai-in-progress")

        from ..execution_grid import AgentExecution, ExecutionStatus
        execution = AgentExecution(id=execution_id, repo_url=config.repo_url, prompt=prompt)
        await self._db.create_execution(execution, issue_id=issue_id)
        logger.info(f"Issue #{issue_id}: retry #{retry_count + 1} — launched agent {execution_id}")

    async def _check_in_progress(self, repo: str) -> None:
        """Phase 4: Check in-progress executions for timeouts."""
        from ..execution_grid import ExecutionStatus
        running = await self._db.get_running_executions()

        for execution in running:
            if execution.started_at:
                from datetime import datetime, timezone
                elapsed = (datetime.now(timezone.utc) - execution.started_at).total_seconds()
                if elapsed > settings.execution_timeout_seconds:
                    logger.warning(f"Execution {execution.id} timed out after {elapsed:.0f}s")
                    # TODO: kill Fly machine if applicable
                    execution.status = ExecutionStatus.FAILED
                    execution.result = "Timed out"
                    await self._db.update_execution(execution)

    async def run_once(self) -> None:
        """Run a single cycle (for testing)."""
        await self.run_cycle()


# Global instance
_management_loop: ManagementLoop | None = None


def get_management_loop() -> ManagementLoop:
    global _management_loop
    if _management_loop is None:
        _management_loop = ManagementLoop()
    return _management_loop
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/management_loop.py
git commit -m "feat: rewrite management loop as 7-phase cron (scan, classify, plan, monitor, review, feedback, unblock)"
```

---

## Task 15: Update webhook handler for PR events

**Files:**
- Modify: `src/agent_grid/issue_tracker/webhook_handler.py`

**Step 1: Add PR webhook handlers**

Add handlers for `pull_request_review`, `pull_request_review_comment`, and `pull_request` events to `webhook_handler.py`. Add these new event types to `EventType` in `execution_grid/public_api.py`:

```python
# Add to EventType enum:
PR_REVIEW = "pr.review"
PR_CLOSED = "pr.closed"
```

Add to webhook handler:

```python
elif x_github_event == "pull_request_review":
    await _handle_pr_review_event(data)
elif x_github_event == "pull_request":
    await _handle_pr_event(data)


async def _handle_pr_review_event(data: dict) -> None:
    """Handle PR review submission."""
    action = data.get("action")
    if action != "submitted":
        return
    pr = data.get("pull_request", {})
    head_branch = pr.get("head", {}).get("ref", "")
    if not head_branch.startswith("agent/"):
        return
    # The cron loop will pick this up via PR monitor
    # But we can trigger an immediate check via event
    repo = data.get("repository", {}).get("full_name", "")
    await event_bus.publish(
        EventType.PR_REVIEW,
        {
            "repo": repo,
            "pr_number": pr.get("number"),
            "branch": head_branch,
            "review_state": data.get("review", {}).get("state"),
        },
    )


async def _handle_pr_event(data: dict) -> None:
    """Handle PR closed/merged events."""
    action = data.get("action")
    if action != "closed":
        return
    pr = data.get("pull_request", {})
    head_branch = pr.get("head", {}).get("ref", "")
    if not head_branch.startswith("agent/"):
        return
    repo = data.get("repository", {}).get("full_name", "")
    merged = pr.get("merged", False)
    await event_bus.publish(
        EventType.PR_CLOSED,
        {
            "repo": repo,
            "pr_number": pr.get("number"),
            "branch": head_branch,
            "merged": merged,
        },
    )
```

**Step 2: Commit**

```bash
git add src/agent_grid/issue_tracker/webhook_handler.py src/agent_grid/execution_grid/public_api.py
git commit -m "feat: add PR review and PR closed webhook handlers"
```

---

## Task 16: Update coordinator __init__.py and main.py

**Files:**
- Modify: `src/agent_grid/coordinator/__init__.py`
- Modify: `src/agent_grid/main.py`

**Step 1: Export new modules from coordinator**

Update `src/agent_grid/coordinator/__init__.py` to export all new modules.

**Step 2: Update main.py lifespan**

- Remove SQS grid startup code
- Add Fly grid startup for coordinator mode
- Keep the management loop (it's now the 7-phase cron)
- Ensure the Fly client is closed on shutdown

**Step 3: Commit**

```bash
git add src/agent_grid/coordinator/__init__.py src/agent_grid/main.py
git commit -m "feat: wire new coordinator modules into app startup"
```

---

## Task 17: Worker Dockerfile for Fly Machines

**Files:**
- Create: `Dockerfile.worker`
- Create: `scripts/worker-entrypoint.sh`

**Step 1: Create worker Dockerfile**

```dockerfile
# Dockerfile.worker — Ephemeral worker for Fly Machines
FROM python:3.12-slim

WORKDIR /workspace

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl jq gh \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (required for Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Python dependencies (only what the worker needs)
RUN pip install --no-cache-dir claude-code-sdk httpx

# Copy entrypoint
COPY scripts/worker-entrypoint.sh /worker-entrypoint.sh
RUN chmod +x /worker-entrypoint.sh

ENTRYPOINT ["/worker-entrypoint.sh"]
```

**Step 2: Create worker entrypoint**

```bash
#!/bin/bash
# worker-entrypoint.sh — Runs on each Fly Machine
set -e

echo "=== Agent Grid Worker ==="
echo "Execution: $EXECUTION_ID"
echo "Repo: $REPO_URL"
echo "Issue: $ISSUE_NUMBER"
echo "Mode: $MODE"

# Configure git
git config --global user.name "Agent Grid"
git config --global user.email "agent-grid@noreply.github.com"

# Configure gh CLI auth
echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null || true

# Clone repo
git clone "$REPO_URL" /workspace/repo
cd /workspace/repo

# Run Claude Code SDK via Python
python3 -c "
import asyncio, json, os, sys
from claude_code_sdk import query
from claude_code_sdk.types import ClaudeCodeOptions, ResultMessage

async def main():
    prompt = os.environ['PROMPT']
    options = ClaudeCodeOptions(
        cwd='/workspace/repo',
        permission_mode='bypassPermissions',
    )

    result = ''
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage) and message.result:
            result = message.result

    # Report back to orchestrator
    import httpx
    callback_url = os.environ.get('ORCHESTRATOR_URL', '') + '/api/agent-status'
    payload = {
        'execution_id': os.environ['EXECUTION_ID'],
        'status': 'completed',
        'result': result[:10000],  # Truncate large results
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(callback_url, json=payload)
            print(f'Reported status: {resp.status_code}')
    except Exception as e:
        print(f'Failed to report status: {e}')
        sys.exit(1)

asyncio.run(main())
" 2>&1 || {
    # Report failure
    python3 -c "
import asyncio, os, sys
import httpx

async def report_failure():
    callback_url = os.environ.get('ORCHESTRATOR_URL', '') + '/api/agent-status'
    payload = {
        'execution_id': os.environ['EXECUTION_ID'],
        'status': 'failed',
        'result': 'Agent process exited with error',
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.post(callback_url, json=payload)

asyncio.run(report_failure())
"
}

echo "=== Worker complete ==="
```

**Step 3: Commit**

```bash
git add Dockerfile.worker scripts/worker-entrypoint.sh
git commit -m "feat: add worker Dockerfile and entrypoint for ephemeral Fly Machines"
```

---

## Task 18: Clean up SQS code

**Files:**
- Delete: `src/agent_grid/execution_grid/sqs_client.py`
- Delete: `src/agent_grid/execution_grid/sqs_grid.py`
- Delete: `src/agent_grid/worker.py`
- Delete: `run-worker.sh`
- Modify: `src/agent_grid/execution_grid/__init__.py` (remove SQS exports)

**Step 1: Remove SQS files and worker**

Delete the files listed above. Update `__init__.py` to remove SQS imports.

**Step 2: Commit**

```bash
git rm src/agent_grid/execution_grid/sqs_client.py \
       src/agent_grid/execution_grid/sqs_grid.py \
       src/agent_grid/worker.py \
       run-worker.sh
git add src/agent_grid/execution_grid/__init__.py
git commit -m "chore: remove SQS-based execution grid and worker (replaced by Fly Machines)"
```

---

## Task 19: Update scheduler to work with new pipeline

**Files:**
- Modify: `src/agent_grid/coordinator/scheduler.py`

**Step 1: Simplify scheduler**

The management loop now handles the scanning/classification/launching pipeline. The scheduler should focus on event-driven reactions (webhooks triggering immediate action, agent completion updating labels, etc.).

Key changes:
- Remove `_generate_prompt` (replaced by prompt_builder.py)
- Update `_handle_agent_completed` to save checkpoints and update labels
- Update `_handle_agent_failed` to update labels
- Keep event subscription for real-time webhook responses

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/scheduler.py
git commit -m "refactor: simplify scheduler — prompt building and scanning moved to management loop"
```

---

## Task 20: Add GitHub PR API methods to GitHubClient

**Files:**
- Modify: `src/agent_grid/issue_tracker/github_client.py`
- Modify: `src/agent_grid/issue_tracker/public_api.py`

**Step 1: Add PR methods to GitHubClient**

Add `list_issues` method with label filtering (already exists) and add `add_label` / `remove_label` as public methods (currently private). Also add `list_issues` to the abstract `IssueTracker` interface.

**Step 2: Commit**

```bash
git add src/agent_grid/issue_tracker/github_client.py src/agent_grid/issue_tracker/public_api.py
git commit -m "feat: expose label management and list_issues on IssueTracker interface"
```

---

## Task 21: fly.toml for the orchestrator (optional, for future Fly deploy)

**Files:**
- Create: `fly.toml`

**Step 1: Create fly.toml**

```toml
app = "agent-grid"
primary_region = "iad"

[build]
  dockerfile = "Dockerfile"

[env]
  AGENT_GRID_HOST = "0.0.0.0"
  AGENT_GRID_PORT = "8000"
  AGENT_GRID_DEPLOYMENT_MODE = "coordinator"

[http_service]
  internal_port = 8000
  force_https = true

[[services]]
  protocol = "tcp"
  internal_port = 8000

  [[services.ports]]
    port = 80
    handlers = ["http"]

  [[services.ports]]
    port = 443
    handlers = ["tls", "http"]
```

**Step 2: Commit**

```bash
git add fly.toml
git commit -m "chore: add fly.toml for orchestrator deployment"
```

---

## Execution Order Summary

Tasks are ordered by dependency:

1. **Task 1** — Config + deps (foundation)
2. **Task 2** — Database schema (needed by everything)
3. **Task 3** — Fly client (needed by execution grid)
4. **Task 4** — Fly execution grid (replaces SQS)
5. **Task 5** — Label manager (needed by scanner/classifier)
6. **Task 6** — Metadata parser (needed by planner/monitor)
7. **Task 7** — Scanner (Phase 1)
8. **Task 8** — Classifier (Phase 2)
9. **Task 9** — Planner (Phase 3)
10. **Task 10** — Prompt builder (needed by management loop)
11. **Task 11** — PR monitor (Phase 5+6)
12. **Task 12** — Blocker resolver (Phase 7)
13. **Task 13** — Dependency resolver
14. **Task 14** — Management loop rewrite (wires everything together)
15. **Task 15** — Webhook handler update
16. **Task 16** — Main.py + __init__.py wiring
17. **Task 17** — Worker Dockerfile for Fly
18. **Task 18** — Remove SQS code
19. **Task 19** — Scheduler refactor
20. **Task 20** — GitHubClient interface updates
21. **Task 21** — fly.toml (optional)

Tasks 7-13 can be done in parallel. Task 14 depends on all of 7-13. Task 18 should be done after Task 4 is verified working.
