"""One-time cleanup: transition stuck ag/in-progress issues to ag/failed.

Issues get stuck when an execution times out or fails but the label is never
transitioned. This script finds all such issues and moves them to ag/failed.

This script uses only the GitHub API (no DB connection required), making it
safe to run from any machine with valid GitHub App credentials
(AGENT_GRID_GITHUB_APP_ID, AGENT_GRID_GITHUB_APP_PRIVATE_KEY,
AGENT_GRID_GITHUB_APP_INSTALLATION_ID).

Usage:
    # Dry-run (default) — shows what would change
    python scripts/cleanup_stuck_issues.py

    # Actually transition labels
    python scripts/cleanup_stuck_issues.py --execute
"""

import argparse
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main(execute: bool) -> None:
    from agent_grid.config import settings
    from agent_grid.issue_tracker import get_issue_tracker
    from agent_grid.issue_tracker.label_manager import get_label_manager
    from agent_grid.issue_tracker.public_api import IssueStatus

    repo = settings.target_repo
    if not repo:
        logger.error("No target_repo configured")
        sys.exit(1)

    logger.info(f"Repo: {repo}")
    logger.info(f"Mode: {'EXECUTE' if execute else 'DRY-RUN'}")

    tracker = get_issue_tracker()
    labels = get_label_manager()

    all_open = await tracker.list_issues(repo, status=IssueStatus.OPEN)
    in_progress = [i for i in all_open if "ag/in-progress" in i.labels]

    logger.info(f"Found {len(in_progress)} issues with ag/in-progress label")

    if not in_progress:
        logger.info("Nothing to clean up!")
        return

    transitioned = 0
    errors = 0
    for issue in in_progress:
        if execute:
            try:
                await labels.transition_to(repo, str(issue.number), "ag/failed")
                logger.info(f"  #{issue.number}: {issue.title} -> ag/failed")
                transitioned += 1
            except Exception as e:
                logger.error(f"  #{issue.number}: FAILED - {e}")
                errors += 1
        else:
            logger.info(f"  [DRY-RUN] #{issue.number}: {issue.title} -> would transition to ag/failed")
            transitioned += 1

    logger.info(f"\nSummary: {transitioned} transitioned, {errors} errors")
    if not execute:
        logger.info("Run with --execute to apply changes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean up stuck ag/in-progress issues")
    parser.add_argument("--execute", action="store_true", help="Actually transition labels (default: dry-run)")
    args = parser.parse_args()
    asyncio.run(main(execute=args.execute))
