"""Issue tracker abstraction layer."""

from .public_api import (
    # Models
    Comment,
    IssueInfo,
    IssueStatus,
    utc_now,
    # ABC interface
    IssueTracker,
    # Service factory
    get_issue_tracker,
    set_issue_tracker,
)
from .github_client import GitHubClient
from .filesystem_client import FilesystemClient
from .webhook_handler import webhook_router
from .api_routes import issues_router

__all__ = [
    # Public API - Models
    "Comment",
    "IssueInfo",
    "IssueStatus",
    "utc_now",
    # Public API - Interface and factory
    "IssueTracker",
    "get_issue_tracker",
    "set_issue_tracker",
    # Implementations
    "GitHubClient",
    "FilesystemClient",
    # Routers
    "webhook_router",
    "issues_router",
]
