"""Phase 5: Monitor agent PRs for human review comments.

Checks open PRs created by agents. When a human leaves review comments,
spawns a new agent to address the feedback on the existing branch.
"""

import logging
from datetime import datetime

from ..issue_tracker import get_issue_tracker
from .database import get_database

logger = logging.getLogger("agent_grid.pr_monitor")


class PRMonitor:
    """Watches agent-created PRs for human review comments."""

    def __init__(self):
        self._tracker = get_issue_tracker()
        self._db = get_database()

    async def check_prs(self, repo: str) -> list[dict]:
        """Check all agent PRs for new review comments.

        Returns list of PRs that need review handling:
        [{"pr_number": N, "issue_id": "...", "review_comments": "...", "branch": "..."}]
        """
        from ..issue_tracker.github_client import GitHubClient

        if not isinstance(self._tracker, GitHubClient):
            return []

        github = self._tracker

        # Get last check time
        last_check_state = await self._db.get_cron_state("last_pr_check")
        last_check = last_check_state.get("timestamp") if last_check_state else None

        # Fetch open PRs with ai-review-pending label
        prs_needing_attention = []

        try:
            response = await github._client.get(
                f"/repos/{repo}/pulls",
                params={"state": "open", "per_page": 100},
            )
            response.raise_for_status()
            prs = response.json()
        except Exception as e:
            logger.error(f"Failed to fetch PRs: {e}")
            return []

        for pr in prs:
            # Only check PRs from agent branches
            head_branch = pr.get("head", {}).get("ref", "")
            if not head_branch.startswith("agent/"):
                continue

            pr_number = pr["number"]

            # Fetch review comments
            try:
                reviews_resp = await github._client.get(
                    f"/repos/{repo}/pulls/{pr_number}/reviews",
                    params={"per_page": 100},
                )
                reviews_resp.raise_for_status()
                reviews = reviews_resp.json()

                comments_resp = await github._client.get(
                    f"/repos/{repo}/pulls/{pr_number}/comments",
                    params={"per_page": 100},
                )
                comments_resp.raise_for_status()
                pr_comments = comments_resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch reviews for PR #{pr_number}: {e}")
                continue

            # Filter for new comments since last check
            new_reviews = []
            for review in reviews:
                if review.get("state") in ("CHANGES_REQUESTED", "COMMENTED") and review.get("body"):
                    if not last_check or review.get("submitted_at", "") > last_check:
                        new_reviews.append(review["body"])

            new_comments = []
            for comment in pr_comments:
                if not last_check or comment.get("created_at", "") > last_check:
                    path = comment.get("path", "")
                    body = comment.get("body", "")
                    new_comments.append(f"File: {path}\n{body}")

            if new_reviews or new_comments:
                all_feedback = "\n\n---\n\n".join(new_reviews + new_comments)

                # Extract linked issue number from PR body
                pr_body = pr.get("body", "") or ""
                issue_id = self._extract_issue_number(pr_body)

                prs_needing_attention.append(
                    {
                        "pr_number": pr_number,
                        "issue_id": issue_id,
                        "review_comments": all_feedback,
                        "branch": head_branch,
                    }
                )

        # Update last check timestamp
        await self._db.set_cron_state(
            "last_pr_check",
            {"timestamp": datetime.utcnow().isoformat()},
        )

        return prs_needing_attention

    async def check_closed_prs(self, repo: str) -> list[dict]:
        """Check recently closed (not merged) PRs for feedback (Phase 6).

        Returns list of closed PRs with human feedback.
        """
        from ..issue_tracker.github_client import GitHubClient

        if not isinstance(self._tracker, GitHubClient):
            return []

        github = self._tracker
        last_check_state = await self._db.get_cron_state("last_closed_pr_check")
        last_check = last_check_state.get("timestamp") if last_check_state else None

        prs_with_feedback = []

        try:
            response = await github._client.get(
                f"/repos/{repo}/pulls",
                params={"state": "closed", "per_page": 50, "sort": "updated", "direction": "desc"},
            )
            response.raise_for_status()
            prs = response.json()
        except Exception as e:
            logger.error(f"Failed to fetch closed PRs: {e}")
            return []

        for pr in prs:
            head_branch = pr.get("head", {}).get("ref", "")
            if not head_branch.startswith("agent/"):
                continue
            if pr.get("merged_at"):
                continue  # Skip merged PRs

            pr_number = pr["number"]
            closed_at = pr.get("closed_at", "")

            if last_check and closed_at <= last_check:
                continue

            # Get comments after close
            try:
                comments_resp = await github._client.get(
                    f"/repos/{repo}/issues/{pr_number}/comments",
                    params={"per_page": 50, "since": closed_at},
                )
                comments_resp.raise_for_status()
                comments = comments_resp.json()
            except Exception:
                continue

            feedback = [c["body"] for c in comments if c.get("body")]
            if not feedback:
                continue

            pr_body = pr.get("body", "") or ""
            issue_id = self._extract_issue_number(pr_body)

            prs_with_feedback.append(
                {
                    "pr_number": pr_number,
                    "issue_id": issue_id,
                    "human_feedback": "\n\n".join(feedback),
                    "branch": head_branch,
                }
            )

        await self._db.set_cron_state(
            "last_closed_pr_check",
            {"timestamp": datetime.utcnow().isoformat()},
        )

        return prs_with_feedback

    def _extract_issue_number(self, pr_body: str) -> str | None:
        """Extract linked issue number from PR body (Closes #N)."""
        import re

        match = re.search(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", pr_body, re.IGNORECASE)
        return match.group(1) if match else None


_pr_monitor: PRMonitor | None = None


def get_pr_monitor() -> PRMonitor:
    global _pr_monitor
    if _pr_monitor is None:
        _pr_monitor = PRMonitor()
    return _pr_monitor
