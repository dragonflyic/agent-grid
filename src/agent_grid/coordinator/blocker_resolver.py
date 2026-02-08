"""Phase 7: Monitor ag/blocked issues for human responses.

When a human responds to a blocked issue, remove ag/blocked label
so the scanner picks it up again next cycle.
"""

import logging

from ..issue_tracker import get_issue_tracker
from ..issue_tracker.label_manager import get_label_manager
from ..issue_tracker.metadata import extract_metadata
from .database import get_database

logger = logging.getLogger("agent_grid.blocker_resolver")


class BlockerResolver:
    """Resolves blocked issues when humans respond."""

    def __init__(self):
        self._tracker = get_issue_tracker()
        self._labels = get_label_manager()
        self._db = get_database()

    async def check_blocked_issues(self, repo: str) -> list[str]:
        """Check ag/blocked issues for human replies after the agent's question.

        Logic: find the agent's blocking comment (has TECH_LEAD_AGENT_META with
        type=blocked), then check if any comment after it is from a human
        (no TECH_LEAD_AGENT_META). If so, unblock.

        Returns list of issue IDs that were unblocked.
        """
        from ..issue_tracker.github_client import GitHubClient

        if not isinstance(self._tracker, GitHubClient):
            return []

        # list_issues doesn't fetch comments, so we get IDs first
        blocked_issues = await self._tracker.list_issues(
            repo,
            labels=["ag/blocked"],
        )

        unblocked = []

        for brief in blocked_issues:
            # Fetch full issue with comments
            issue = await self._tracker.get_issue(repo, brief.id)

            if self._has_human_reply_after_block(issue.comments):
                await self._labels.transition_to(repo, issue.id, "ag/todo")
                logger.info(f"Unblocked issue #{issue.number} — human responded")
                unblocked.append(issue.id)

        return unblocked

    def _has_human_reply_after_block(self, comments: list) -> bool:
        """Check if a human replied after the agent's blocking comment."""
        # Find the last agent blocking comment
        last_block_idx = None
        for i, comment in enumerate(comments):
            meta = extract_metadata(comment.body)
            if meta and meta.get("type") == "blocked":
                last_block_idx = i

        if last_block_idx is None:
            return False

        # Check if any comment after it is from a human (no agent metadata)
        for comment in comments[last_block_idx + 1 :]:
            if extract_metadata(comment.body) is None:
                return True

        return False


_blocker_resolver: BlockerResolver | None = None


def get_blocker_resolver() -> BlockerResolver:
    global _blocker_resolver
    if _blocker_resolver is None:
        _blocker_resolver = BlockerResolver()
    return _blocker_resolver
