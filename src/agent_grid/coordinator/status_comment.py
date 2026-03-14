"""Post and update a single status comment on GitHub issues.

Uses an HTML marker (<!-- agent-grid-status -->) to find its own comment
and update it in-place, avoiding comment spam.
"""

import logging

from ..issue_tracker import get_issue_tracker
from .database import get_database

logger = logging.getLogger("agent_grid.status_comment")

MARKER = "<!-- agent-grid-status -->"
METADATA_KEY = "status_comment_id"


def _render_status(stage: str, detail: str | None = None) -> str:
    """Render the status comment body with the marker."""
    status_map = {
        "launched": ("Working on it", "An agent has started working on this issue."),
        "planning": ("Planning", "An agent is decomposing this into sub-issues."),
        "in_progress": ("In progress", "An agent is working on this issue."),
        "review_pending": (
            "Review pending",
            "Implementation is ready for review.",
        ),
        "pr_created": ("PR created", "A pull request has been created."),
        "ci_fix": ("Fixing CI", "An agent is fixing a CI failure."),
        "addressing_review": (
            "Addressing review",
            "An agent is addressing review comments.",
        ),
        "retrying": ("Retrying", "An agent is retrying with feedback."),
        "scouting": ("Scouting", "An agent is exploring the codebase and planning the approach."),
        "rebasing": ("Rebasing", "An agent is rebasing the branch to resolve merge conflicts."),
        "completed": ("Done", "The issue has been resolved."),
        "failed": ("Failed", "The agent was unable to resolve this issue."),
        "pr_merged": ("Merged", "The pull request has been merged."),
    }

    emoji_map = {
        "launched": "\U0001f680",
        "planning": "\U0001f4cb",
        "in_progress": "\u2699\ufe0f",
        "review_pending": "\U0001f440",
        "pr_created": "\U0001f4e6",
        "ci_fix": "\U0001f527",
        "addressing_review": "\U0001f4dd",
        "retrying": "\U0001f504",
        "scouting": "\U0001f50d",
        "rebasing": "\U0001f500",
        "completed": "\u2705",
        "failed": "\u274c",
        "pr_merged": "\U0001f389",
    }

    title, default_detail = status_map.get(stage, ("Update", "Status updated."))
    emoji = emoji_map.get(stage, "\U0001f916")
    body = detail or default_detail

    return f"""{MARKER}
{emoji} **agent-grid status: {title}**

{body}

---
<sub>Updated by [agent-grid](https://github.com/apps/agent-grid) | [Dashboard](https://agent-grid.fly.dev)</sub>"""


def _extract_comment_id(raw_metadata) -> str | None:
    """Extract status_comment_id from metadata, handling dict, list, or string forms."""
    if raw_metadata is None:
        return None
    if isinstance(raw_metadata, str):
        import json

        try:
            raw_metadata = json.loads(raw_metadata)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(raw_metadata, dict):
        return raw_metadata.get(METADATA_KEY)
    if isinstance(raw_metadata, list):
        for item in raw_metadata:
            if isinstance(item, dict) and METADATA_KEY in item:
                return item[METADATA_KEY]
    return None


class StatusCommentManager:
    """Manages a single status comment per issue."""

    async def post_or_update(
        self,
        repo: str,
        issue_id: str,
        stage: str,
        detail: str | None = None,
    ) -> None:
        """Post a new status comment or update the existing one."""
        body = _render_status(stage, detail)

        db = get_database()
        tracker = get_issue_tracker()

        # Check if we already have a comment ID stored in metadata
        issue_number = int(issue_id)
        state = await db.get_issue_state(issue_number, repo)
        raw_metadata = (state or {}).get("metadata")
        comment_id = _extract_comment_id(raw_metadata)

        if comment_id:
            try:
                await tracker.update_comment(repo, str(comment_id), body)
                logger.debug(f"Updated status comment {comment_id} on issue #{issue_id}")
                return
            except Exception:
                # Comment may have been deleted — fall through to create new one
                logger.warning(f"Failed to update comment {comment_id}, creating new one")

        # Create a new comment
        try:
            new_id = await tracker.add_comment(repo, issue_id, body)
            if new_id:
                await db.merge_issue_metadata(
                    issue_number=issue_number,
                    repo=repo,
                    metadata_update={METADATA_KEY: new_id},
                )
                logger.debug(f"Created status comment {new_id} on issue #{issue_id}")
        except Exception:
            logger.warning(f"Failed to post status comment on issue #{issue_id}", exc_info=True)


_status_comment_manager: StatusCommentManager | None = None


def get_status_comment_manager() -> StatusCommentManager:
    global _status_comment_manager
    if _status_comment_manager is None:
        _status_comment_manager = StatusCommentManager()
    return _status_comment_manager
