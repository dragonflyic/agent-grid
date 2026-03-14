"""Public API for issue tracker module.

This module defines the public interface and models for the issue tracker.
Implementation modules import from here, not the other way around.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

# =============================================================================
# Utilities
# =============================================================================


def utc_now() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


# =============================================================================
# Models
# =============================================================================


class IssueStatus(str, Enum):
    """Status of an issue."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


class Comment(BaseModel):
    """A comment on an issue."""

    id: str
    body: str
    author: str = ""
    author_type: str = ""  # "User" or "Bot"
    created_at: datetime = Field(default_factory=utc_now)


class IssueInfo(BaseModel):
    """Information about an issue from the issue tracker."""

    id: str
    number: int
    title: str
    body: str | None = None
    author: str = ""
    status: IssueStatus = IssueStatus.OPEN
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    node_id: str | None = None
    repo_url: str
    html_url: str
    parent_id: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


# =============================================================================
# Service Interface (ABC)
# =============================================================================


class IssueTracker(ABC):
    """Abstract interface for issue tracking systems."""

    @abstractmethod
    async def get_issue(self, repo: str, issue_id: str) -> IssueInfo:
        """
        Get information about an issue.

        Args:
            repo: Repository in owner/name format.
            issue_id: Issue number or ID.

        Returns:
            IssueInfo with issue details.
        """
        pass

    @abstractmethod
    async def list_subissues(self, repo: str, parent_id: str) -> list[IssueInfo]:
        """
        List all subissues of a parent issue.

        Args:
            repo: Repository in owner/name format.
            parent_id: Parent issue number or ID.

        Returns:
            List of IssueInfo for subissues.
        """
        pass

    @abstractmethod
    async def create_subissue(
        self,
        repo: str,
        parent_id: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> IssueInfo:
        """
        Create a subissue under a parent issue.

        Args:
            repo: Repository in owner/name format.
            parent_id: Parent issue number or ID.
            title: Issue title.
            body: Issue body/description.
            labels: Optional labels to apply.

        Returns:
            IssueInfo for the created subissue.
        """
        pass

    @abstractmethod
    async def list_issues(
        self,
        repo: str,
        status: IssueStatus | None = None,
        labels: list[str] | None = None,
    ) -> list[IssueInfo]:
        """
        List issues with optional filters.

        Args:
            repo: Repository in owner/name format.
            status: Filter by issue status.
            labels: Filter by labels.

        Returns:
            List of matching IssueInfo.
        """
        pass

    @abstractmethod
    async def add_comment(self, repo: str, issue_id: str, body: str) -> str | None:
        """
        Add a comment to an issue.

        Args:
            repo: Repository in owner/name format.
            issue_id: Issue number or ID.
            body: Comment body.

        Returns:
            Comment ID as string, or None.
        """
        pass

    async def update_comment(self, repo: str, comment_id: str, body: str) -> None:
        """
        Update an existing comment by ID.

        Args:
            repo: Repository in owner/name format.
            comment_id: Comment ID.
            body: New comment body.
        """
        pass

    @abstractmethod
    async def update_issue_status(self, repo: str, issue_id: str, status: IssueStatus) -> None:
        """
        Update the status of an issue.

        Args:
            repo: Repository in owner/name format.
            issue_id: Issue number or ID.
            status: New status.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close any open connections."""
        pass

    # -----------------------------------------------------------------
    # Non-abstract methods with sensible defaults.
    # Override in concrete implementations (e.g. GitHubClient).
    # -----------------------------------------------------------------

    async def list_open_prs(self, repo: str, **params) -> list[dict]:
        """List open pull requests. Returns raw PR dicts."""
        return []

    async def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict]:
        """Get reviews for a pull request."""
        return []

    async def get_pr_comments(self, repo: str, pr_number: int) -> list[dict]:
        """Get inline/review comments for a pull request."""
        return []

    async def get_pr_by_branch(self, repo: str, branch: str) -> dict | None:
        """Find an open PR for the given head branch."""
        return None

    async def add_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        """Post a comment on a pull request."""
        pass

    async def request_pr_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
        """Request reviewers on a pull request."""
        pass

    async def add_label(self, repo: str, issue_id: str, label: str) -> None:
        """Add a label to an issue."""
        pass

    async def remove_label(self, repo: str, issue_id: str, label: str) -> None:
        """Remove a label from an issue."""
        pass

    async def get_check_runs_for_ref(self, repo: str, ref: str, *, status: str = "completed") -> list[dict]:
        """Return check runs for a commit SHA or branch ref."""
        return []

    async def get_actions_job_logs(self, repo: str, job_id: int) -> str:
        """Download logs for a GitHub Actions job."""
        return ""

    async def assign_issue(self, repo: str, issue_id: str, assignee: str) -> None:
        """Assign an issue to a user."""
        pass

    async def get_pr_data(self, repo: str, pr_number: int) -> dict | None:
        """Fetch a single PR by number."""
        return None

    async def get_issue_comments_since(self, repo: str, issue_id: str, since: str | None = None) -> list[dict]:
        """Fetch issue comments, optionally since a timestamp."""
        return []

    async def create_label(self, repo: str, name: str, color: str) -> bool:
        """Create a label in the repo. Returns True if created, False if already exists."""
        return False

    async def get_reference_status(self, repo: str, ref_num: str) -> dict:
        """Get the status of a referenced issue or PR.

        Returns dict with keys: title, status ("OPEN", "CLOSED", or "MERGED").
        Default implementation uses get_issue(). GitHub client overrides to
        detect merged PRs.
        """
        try:
            info = await self.get_issue(repo, ref_num)
            return {"title": info.title, "status": info.status.value.upper()}
        except Exception:
            return {"title": "", "status": "UNKNOWN"}


# =============================================================================
# Service Factory
# =============================================================================

# Singleton instance
_issue_tracker: IssueTracker | None = None


def get_issue_tracker() -> IssueTracker:
    """Get the global issue tracker instance based on configuration."""
    global _issue_tracker
    if _issue_tracker is None:
        from ..config import settings

        if settings.issue_tracker_type == "filesystem":
            from .filesystem_client import FilesystemClient

            _issue_tracker = FilesystemClient()
        else:
            from .github_client import GitHubClient

            _issue_tracker = GitHubClient()
    return _issue_tracker


async def set_issue_tracker(tracker: IssueTracker) -> None:
    """Set the global issue tracker instance (for testing)."""
    global _issue_tracker
    if _issue_tracker is not None:
        await _issue_tracker.close()
    _issue_tracker = tracker
