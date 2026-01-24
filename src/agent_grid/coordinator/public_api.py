"""Public API for coordinator module - FastAPI routes."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException

from ..common.models import (
    AgentExecution,
    ExecutionStatus,
    NudgeRequest,
    NudgeRequestCreate,
)
from .budget_manager import get_budget_manager
from .database import get_database
from .nudge_handler import get_nudge_handler

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
    handler = get_nudge_handler()
    return await handler.handle_nudge(
        issue_id=request.issue_id,
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
    handler = get_nudge_handler()
    return await handler.get_pending_nudges(limit)


@coordinator_router.get("/budget")
async def get_budget_status() -> dict[str, Any]:
    """
    Get current budget status.

    Returns:
        Budget status including concurrent executions and resource usage.
    """
    manager = get_budget_manager()
    return await manager.get_budget_status()
