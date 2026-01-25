"""API routes for issue tracker operations.

These routes provide a REST API for issue operations, primarily used
by agents to create subissues during planning. When using the GitHub
issue tracker, agents would use the GitHub API directly instead.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .public_api import IssueInfo, get_issue_tracker

issues_router = APIRouter(prefix="/api/issues", tags=["issues"])


class CreateSubissueRequest(BaseModel):
    """Request to create a subissue."""

    title: str
    body: str
    labels: list[str] | None = None


@issues_router.post("/{repo:path}/{parent_id}/subissues")
async def create_subissue(
    repo: str,
    parent_id: str,
    request: CreateSubissueRequest,
) -> IssueInfo:
    """
    Create a subissue under a parent issue.

    This endpoint is used by planning agents to decompose large issues
    into smaller, actionable subissues.

    Args:
        repo: Repository in owner/name format (e.g., "owner/repo").
        parent_id: Parent issue number or ID.
        request: The subissue details.

    Returns:
        The created subissue info.

    Raises:
        HTTPException: If the parent issue is not found.
    """
    tracker = get_issue_tracker()

    # Verify parent issue exists
    try:
        await tracker.get_issue(repo, parent_id)
    except (FileNotFoundError, Exception) as e:
        raise HTTPException(status_code=404, detail=f"Parent issue {parent_id} not found: {e}")

    # Create the subissue
    subissue = await tracker.create_subissue(
        repo=repo,
        parent_id=parent_id,
        title=request.title,
        body=request.body,
        labels=request.labels,
    )

    return subissue


@issues_router.get("/{repo:path}/{issue_id}")
async def get_issue(repo: str, issue_id: str) -> IssueInfo:
    """
    Get issue details.

    Args:
        repo: Repository in owner/name format.
        issue_id: Issue number or ID.

    Returns:
        The issue info.

    Raises:
        HTTPException: If issue not found.
    """
    tracker = get_issue_tracker()
    try:
        return await tracker.get_issue(repo, issue_id)
    except (FileNotFoundError, Exception) as e:
        raise HTTPException(status_code=404, detail=f"Issue {issue_id} not found: {e}")
