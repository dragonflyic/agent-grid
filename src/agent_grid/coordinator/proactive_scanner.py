"""Proactive scanner: discover issues suitable for automation without ag/* labels.

Scans all open issues in the target repo, filters out those already in the
ag/* pipeline or previously evaluated, and returns candidates for the
quality gate to assess.
"""

import logging

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.public_api import IssueInfo, IssueStatus
from .database import ensure_metadata_dict, get_database

logger = logging.getLogger("agent_grid.proactive_scanner")

AG_PREFIX = "ag/"


class ProactiveScanner:
    """Scans for open issues without ag/* labels."""

    def __init__(self):
        self._tracker = get_issue_tracker()
        self._db = get_database()

    async def scan(self, repo: str | None = None) -> list[IssueInfo]:
        """Scan for open issues that have no ag/* labels.

        Filters out:
        - Issues with any ag/* label (already in the pipeline)
        - Issues previously evaluated and skipped by the proactive scanner
        - Issues previously picked up by the proactive scanner
        """
        repo = repo or settings.target_repo
        if not repo:
            logger.warning("No target_repo configured")
            return []

        all_open = await self._tracker.list_issues(repo, status=IssueStatus.OPEN)

        candidates = []
        for issue in all_open:
            # Skip issues already in the ag/* pipeline
            if any(label.startswith(AG_PREFIX) for label in issue.labels):
                continue

            # Check if we already evaluated this issue
            issue_state = await self._db.get_issue_state(issue.number, repo)
            if issue_state:
                metadata = ensure_metadata_dict(issue_state.get("metadata"))
                if metadata.get("proactive_skipped"):
                    continue
                if metadata.get("proactive_picked"):
                    continue

            candidates.append(issue)

        logger.info(
            f"Proactive scan {repo}: {len(all_open)} open issues, {len(candidates)} candidates without ag/* labels"
        )
        return candidates


_proactive_scanner: ProactiveScanner | None = None


def get_proactive_scanner() -> ProactiveScanner:
    global _proactive_scanner
    if _proactive_scanner is None:
        _proactive_scanner = ProactiveScanner()
    return _proactive_scanner
