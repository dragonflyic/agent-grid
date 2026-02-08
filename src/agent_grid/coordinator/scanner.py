"""Phase 1: Scan GitHub for open issues to process.

Fetches all open issues from the target repo, filters out those
already being handled (via ai-* labels), and returns candidates.
"""

import logging

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.public_api import IssueInfo, IssueStatus

logger = logging.getLogger("agent_grid.scanner")

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

        Returns issues that are open and have no ai-* labels.
        """
        repo = repo or settings.target_repo
        if not repo:
            logger.warning("No target_repo configured")
            return []

        all_open = await self._tracker.list_issues(repo, status=IssueStatus.OPEN)

        candidates = []
        for issue in all_open:
            if any(label in HANDLED_LABELS for label in issue.labels):
                continue
            candidates.append(issue)

        logger.info(f"Scanned {repo}: {len(all_open)} open issues, {len(candidates)} candidates")
        return candidates


_scanner: Scanner | None = None


def get_scanner() -> Scanner:
    global _scanner
    if _scanner is None:
        _scanner = Scanner()
    return _scanner
