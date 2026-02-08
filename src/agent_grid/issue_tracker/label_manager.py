"""Label lifecycle management for the Tech Lead Agent.

Manages ag/* labels on GitHub issues to track pipeline state.
"""

import logging

from .github_client import GitHubClient
from .public_api import get_issue_tracker

logger = logging.getLogger("agent_grid.labels")

# All labels managed by the system
AG_LABELS = {
    "ag/todo",
    "ag/in-progress",
    "ag/blocked",
    "ag/waiting",
    "ag/planning",
    "ag/review-pending",
    "ag/done",
    "ag/failed",
    "ag/skipped",
    "ag/sub-issue",
    "ag/epic",
}


class LabelManager:
    """Manages label transitions on GitHub issues."""

    def __init__(self):
        tracker = get_issue_tracker()
        if not isinstance(tracker, GitHubClient):
            raise TypeError("LabelManager requires GitHubClient")
        self._github = tracker

    async def transition_to(self, repo: str, issue_id: str, new_label: str) -> None:
        """Remove all ag/* labels and add the new one."""
        issue = await self._github.get_issue(repo, issue_id)
        current_ag_labels = [label for label in issue.labels if label in AG_LABELS]

        for label in current_ag_labels:
            if label != new_label:
                await self._github._remove_label(repo, issue_id, label)

        if new_label not in current_ag_labels:
            await self._github._add_label(repo, issue_id, new_label)

        logger.info(f"Issue #{issue_id}: transitioned to {new_label}")

    async def add_label(self, repo: str, issue_id: str, label: str) -> None:
        """Add a label without removing others."""
        await self._github._add_label(repo, issue_id, label)

    async def remove_label(self, repo: str, issue_id: str, label: str) -> None:
        """Remove a specific label."""
        await self._github._remove_label(repo, issue_id, label)

    async def ensure_labels_exist(self, repo: str) -> None:
        """Create all ag/* labels in the repo if they don't exist."""
        label_colors = {
            "ag/todo": "006b75",
            "ag/in-progress": "1d76db",
            "ag/blocked": "e4e669",
            "ag/waiting": "c5def5",
            "ag/planning": "d4c5f9",
            "ag/review-pending": "fbca04",
            "ag/done": "0e8a16",
            "ag/failed": "d93f0b",
            "ag/skipped": "cccccc",
            "ag/sub-issue": "bfdadc",
            "ag/epic": "3e4b9e",
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
