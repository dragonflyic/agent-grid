"""Issue tracker abstraction layer."""

from .api_routes import issues_router
from .filesystem_client import FilesystemClient
from .github_client import GitHubClient
from .label_manager import AI_LABELS, LabelManager, get_label_manager
from .public_api import (
    # Models
    Comment,
    IssueInfo,
    IssueStatus,
    # ABC interface
    IssueTracker,
    # Service factory
    get_issue_tracker,
    set_issue_tracker,
    utc_now,
)
from .webhook_handler import webhook_router

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
    # Label management
    "AI_LABELS",
    "LabelManager",
    "get_label_manager",
    # Routers
    "webhook_router",
    "issues_router",
]
