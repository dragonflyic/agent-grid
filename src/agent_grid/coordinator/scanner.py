"""Phase 1: Scan GitHub for open issues to process.

Fetches all open issues from the target repo, filters to only those
opted-in via ag/* labels, and returns candidates not yet being handled.
"""

import logging

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.public_api import IssueInfo, IssueStatus

logger = logging.getLogger("agent_grid.scanner")

# Labels that indicate an issue is already being handled
HANDLED_LABELS = {
    "ag/in-progress",
    "ag/blocked",
    "ag/waiting",
    "ag/planning",
    "ag/review-pending",
    "ag/done",
    "ag/failed",
    "ag/skipped",
    "ag/epic",
    "ag/sub-issue",
}

AG_PREFIX = "ag/"


class Scanner:
    """Scans GitHub for unprocessed open issues."""

    def __init__(self):
        self._tracker = get_issue_tracker()

    async def scan(self, repo: str | None = None) -> list[IssueInfo]:
        """Scan for open issues that need processing.

        Only considers issues that have been opted-in with an ag/* label.
        Filters out issues already being handled (ag/in-progress, ag/blocked, etc.).
        """
        repo = repo or settings.target_repo
        if not repo:
            logger.warning("No target_repo configured")
            return []

        all_open = await self._tracker.list_issues(repo, status=IssueStatus.OPEN)

        candidates = []
        for issue in all_open:
            # Only consider issues opted-in with an ag/ label
            has_ag_label = any(label.startswith(AG_PREFIX) for label in issue.labels)
            if not has_ag_label:
                continue

            # Skip issues already being handled
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
