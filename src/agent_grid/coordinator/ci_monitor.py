"""CI failure polling — backup to webhook delivery.

Checks open agent PRs for failing CI checks. Uses cron_state cursor
to avoid re-processing the same failures across cycles.
"""

import json
import logging
import re
from datetime import datetime, timezone

from ..issue_tracker import get_issue_tracker
from .database import get_database
from .pr_monitor import _normalize_timestamp

logger = logging.getLogger("agent_grid.ci_monitor")


class CIMonitor:
    """Polls GitHub for CI failures on agent branches (backup to webhooks)."""

    def __init__(self):
        self._tracker = get_issue_tracker()
        self._db = get_database()

    async def check_ci_failures(self, repo: str) -> list[dict]:
        """Check open agent PRs for failing CI checks.

        Returns list of check_info dicts matching the CHECK_RUN_FAILED payload shape,
        suitable for passing directly to ManagementLoop._launch_ci_fix().
        """
        # Fetch open PRs
        prs = await self._tracker.list_open_prs(repo, per_page=100)

        failures = []

        for pr in prs:
            head_branch = pr.get("head", {}).get("ref", "")
            if not head_branch.startswith("agent/"):
                continue

            match = re.match(r"agent/(\d+)(?:-|$)", head_branch)
            if not match:
                continue
            issue_id = match.group(1)

            head_sha = pr.get("head", {}).get("sha", "")
            pr_number = pr["number"]

            # Dedup: skip if we already processed this SHA
            issue_state = await self._db.get_issue_state(int(issue_id), repo)
            metadata = (issue_state or {}).get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            if metadata.get("last_ci_check_sha") == head_sha:
                continue

            # Fetch check runs for this commit
            check_runs = await self._tracker.get_check_runs_for_ref(repo, head_sha)
            for cr in check_runs:
                if cr.get("conclusion") not in ("failure", "timed_out"):
                    continue

                failures.append(
                    {
                        "repo": repo,
                        "branch": head_branch,
                        "head_sha": head_sha,
                        "pr_number": pr_number,
                        "check_name": cr.get("name", ""),
                        "check_output": "",  # will be fetched via job logs
                        "check_url": cr.get("html_url", ""),
                        "job_id": cr.get("id"),
                    }
                )
                break  # One failure per PR is enough to trigger a fix agent

        # Update cursor
        await self._db.set_cron_state(
            "last_ci_poll",
            {"timestamp": _normalize_timestamp(datetime.now(timezone.utc).isoformat())},
        )

        if failures:
            logger.info(f"CIMonitor: found {len(failures)} CI failures on agent branches")

        return failures


_ci_monitor: CIMonitor | None = None


def get_ci_monitor() -> CIMonitor:
    global _ci_monitor
    if _ci_monitor is None:
        _ci_monitor = CIMonitor()
    return _ci_monitor
