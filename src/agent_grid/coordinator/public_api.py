"""Public API for coordinator module.

This module defines the public interface and models for the coordinator.
Contains FastAPI routes and request/response models.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..execution_grid import AgentExecution, ExecutionStatus

# =============================================================================
# Utilities
# =============================================================================


def utc_now() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


# =============================================================================
# Models
# =============================================================================


class NudgeRequest(BaseModel):
    """A request from an agent to nudge another issue/agent."""

    id: UUID
    issue_id: str
    source_execution_id: UUID | None = None
    priority: int = 0
    reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    processed_at: datetime | None = None


class NudgeRequestCreate(BaseModel):
    """Request to create a nudge."""

    issue_id: str
    repo: str | None = None  # Repository in owner/name format
    source_execution_id: UUID | None = None
    priority: int = 0
    reason: str | None = None


# =============================================================================
# API Routes
# =============================================================================

coordinator_router = APIRouter(prefix="/api", tags=["coordinator"])


@coordinator_router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


@coordinator_router.get("/executions")
async def list_executions(
    status: ExecutionStatus | None = None,
    issue_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AgentExecution]:
    """
    List executions with optional filters.

    Args:
        status: Filter by execution status.
        issue_id: Filter by issue ID.
        limit: Maximum number of results.
        offset: Offset for pagination.

    Returns:
        List of matching executions.
    """
    from .database import get_database

    db = get_database()
    return await db.list_executions(
        status=status,
        issue_id=issue_id,
        limit=limit,
        offset=offset,
    )


@coordinator_router.get("/executions/{execution_id}")
async def get_execution(execution_id: UUID) -> AgentExecution:
    """
    Get execution details by ID.

    Args:
        execution_id: The execution UUID.

    Returns:
        The execution details.

    Raises:
        HTTPException: If execution not found.
    """
    from .database import get_database

    db = get_database()
    execution = await db.get_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    return execution


@coordinator_router.post("/nudge")
async def create_nudge(request: NudgeRequestCreate) -> NudgeRequest:
    """
    Create a nudge request.

    Nudges are requests from agents to start work on other issues.

    Args:
        request: The nudge request details.

    Returns:
        The created nudge request.
    """
    from .nudge_handler import get_nudge_handler

    handler = get_nudge_handler()
    return await handler.handle_nudge(
        issue_id=request.issue_id,
        repo=request.repo,
        source_execution_id=request.source_execution_id,
        priority=request.priority,
        reason=request.reason,
    )


@coordinator_router.get("/nudges")
async def list_pending_nudges(limit: int = 10) -> list[NudgeRequest]:
    """
    List pending nudge requests.

    Args:
        limit: Maximum number of results.

    Returns:
        List of pending nudge requests.
    """
    from .nudge_handler import get_nudge_handler

    handler = get_nudge_handler()
    return await handler.get_pending_nudges(limit)


class AgentStatusCallback(BaseModel):
    """Callback payload from Fly Machine workers."""

    execution_id: str
    status: str  # "completed" or "failed"
    result: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    checkpoint: dict | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    session_s3_key: str | None = None


@coordinator_router.post("/agent-status")
async def agent_status_callback(body: AgentStatusCallback) -> dict[str, str]:
    """Callback endpoint for Fly Machine workers to report results.

    Only active when execution_backend is 'fly'. Oz uses polling instead.
    """
    from ..config import settings

    if settings.execution_backend == "fly":
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
    elif settings.execution_backend in ("claude-code", "oz"):
        from ..execution_grid.claude_code_grid import get_claude_code_execution_grid

        grid = get_claude_code_execution_grid()
        await grid.handle_agent_result(
            execution_id=UUID(body.execution_id),
            status=body.status,
            result=body.result,
            branch=body.branch,
            pr_number=body.pr_number,
            cost_usd=body.cost_usd,
            session_id=body.session_id,
            session_s3_key=body.session_s3_key,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Agent status callback is only available with the Fly or Claude Code execution backend",
        )

    return {"status": "ok"}


@coordinator_router.post("/agent-events")
async def receive_agent_events(request: Request) -> dict:
    """Receive batched agent events from a Fly Machine worker.

    Workers POST arrays of events during execution for real-time observability.
    Events are stored in the agent_events DB table and viewable via the dashboard.
    Non-critical — if this fails, events are still in the worker's events.jsonl on S3.
    """
    from .database import get_database

    db = get_database()
    events = await request.json()

    if not isinstance(events, list):
        events = [events]

    stored = 0
    for event in events:
        try:
            await db.record_agent_event(
                execution_id=UUID(event["execution_id"]),
                message_type=event.get("type", ""),
                content=event.get("content", "")[:10000],
                tool_name=event.get("tool_name"),
                tool_id=event.get("tool_id"),
            )
            stored += 1
        except Exception:
            pass  # Best effort — don't fail the batch for one bad event

    return {"received": len(events), "stored": stored}


@coordinator_router.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: UUID) -> dict[str, str]:
    """Cancel an active execution (stops the backend run and updates DB)."""
    from ..execution_grid import get_execution_grid
    from .database import get_database

    db = get_database()
    execution = await db.get_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.status not in (ExecutionStatus.PENDING, ExecutionStatus.RUNNING):
        raise HTTPException(status_code=400, detail=f"Execution is already {execution.status}")

    # Cancel the actual backend run (Oz/Fly) so it stops burning compute
    grid = get_execution_grid()
    try:
        await grid.cancel_execution(execution_id)
    except Exception:
        pass  # Best-effort; DB update below ensures consistent state

    execution.status = ExecutionStatus.FAILED
    execution.result = "Manually cancelled"
    await db.update_execution(execution)
    return {"status": "cancelled", "execution_id": str(execution_id)}


@coordinator_router.get("/issue-state/{issue_number}")
async def get_issue_state(issue_number: int, repo: str | None = None) -> dict[str, Any]:
    """Get issue state including metadata."""
    from ..config import settings
    from .database import get_database

    db = get_database()
    state = await db.get_issue_state(issue_number, repo or settings.target_repo)
    if not state:
        raise HTTPException(status_code=404, detail="Issue state not found")
    return dict(state)


@coordinator_router.post("/issue-state/{issue_number}/reset-ci")
async def reset_ci_fix_count(issue_number: int, repo: str | None = None) -> dict[str, Any]:
    """Reset the CI fix counter for an issue."""
    from ..config import settings
    from .database import ensure_metadata_dict, get_database

    db = get_database()
    actual_repo = repo or settings.target_repo
    state = await db.get_issue_state(issue_number, actual_repo)
    if not state:
        raise HTTPException(status_code=404, detail="Issue state not found")
    metadata = ensure_metadata_dict(state.get("metadata"))
    metadata.pop("ci_fix_count", None)
    metadata.pop("last_ci_check_sha", None)
    await db.upsert_issue_state(issue_number=issue_number, repo=actual_repo, metadata=metadata)
    return {"status": "reset", "issue_number": issue_number}


@coordinator_router.get("/budget")
async def get_budget_status() -> dict[str, Any]:
    """
    Get current budget status.

    Returns:
        Budget status including concurrent executions and resource usage.
    """
    from .budget_manager import get_budget_manager

    manager = get_budget_manager()
    return await manager.get_budget_status()
