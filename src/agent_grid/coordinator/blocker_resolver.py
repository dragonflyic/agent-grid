"""Phase 7: Monitor ai-blocked issues for human responses.

When a human responds to a blocked issue, remove ai-blocked label
so the scanner picks it up again next cycle.
"""

import logging

from ..issue_tracker import get_issue_tracker
from ..issue_tracker.label_manager import get_label_manager
from .database import get_database

logger = logging.getLogger("agent_grid.blocker_resolver")


class BlockerResolver:
    """Resolves blocked issues when humans respond."""

    def __init__(self):
        self._tracker = get_issue_tracker()
        self._labels = get_label_manager()
        self._db = get_database()

    async def check_blocked_issues(self, repo: str) -> list[str]:
        """Check ai-blocked issues for new human comments.

        Returns list of issue IDs that were unblocked.
        """
        from ..issue_tracker.github_client import GitHubClient

        if not isinstance(self._tracker, GitHubClient):
            return []

        blocked_issues = await self._tracker.list_issues(
            repo,
            labels=["ai-blocked"],
        )

        last_check_state = await self._db.get_cron_state("last_blocker_check")
        last_check = last_check_state.get("timestamp") if last_check_state else None

        unblocked = []

        for issue in blocked_issues:
            # Check if there are new comments since last check
            has_new_comments = False
            for comment in issue.comments:
                comment_time = comment.created_at.isoformat() if comment.created_at else ""
                if not last_check or comment_time > last_check:
                    has_new_comments = True
                    break

            if has_new_comments:
                # Unblock: remove ai-blocked, scanner will pick it up
                await self._labels.remove_label(repo, issue.id, "ai-blocked")
                logger.info(f"Unblocked issue #{issue.number} — human responded")
                unblocked.append(issue.id)

        from datetime import datetime

        await self._db.set_cron_state(
            "last_blocker_check",
            {"timestamp": datetime.utcnow().isoformat()},
        )

        return unblocked


_blocker_resolver: BlockerResolver | None = None


def get_blocker_resolver() -> BlockerResolver:
    global _blocker_resolver
    if _blocker_resolver is None:
        _blocker_resolver = BlockerResolver()
    return _blocker_resolver
