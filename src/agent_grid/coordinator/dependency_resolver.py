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
