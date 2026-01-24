"""Public API for issue tracker abstraction."""

from abc import ABC, abstractmethod

from ..common.models import IssueInfo, IssueStatus


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
    async def add_comment(self, repo: str, issue_id: str, body: str) -> None:
        """
        Add a comment to an issue.

        Args:
            repo: Repository in owner/name format.
            issue_id: Issue number or ID.
            body: Comment body.
        """
        pass

    @abstractmethod
    async def update_issue_status(
        self, repo: str, issue_id: str, status: IssueStatus
    ) -> None:
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
