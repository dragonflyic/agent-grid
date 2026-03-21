"""Claude Code execution grid callbacks — wired during application startup.

Handles DB persistence, fallback PR detection/creation, and cost tracking
when Claude Code CLI workers complete. Same pattern as oz_callbacks.py.
"""

import logging
from uuid import UUID

from ..execution_grid.claude_code_grid import ClaudeCodeCallbacks, RunArtifacts
from ..execution_grid.public_api import ExecutionStatus

logger = logging.getLogger("agent_grid.claude_code_callbacks")


def build_claude_code_callbacks(db, tracker) -> ClaudeCodeCallbacks:
    """Build callbacks wired to the given database and issue tracker."""

    async def on_execution_completed(
        execution_id: UUID, artifacts: RunArtifacts
    ) -> RunArtifacts:
        """Handle successful execution — detect/create PR, persist cost, update DB."""
        # Detect or create PR if agent pushed a branch
        if artifacts.branch and artifacts.pr_number is None:
            try:
                issue_id = await db.get_issue_id_for_execution(execution_id)
                if issue_id:
                    execution = await db.get_execution(execution_id)
                    repo = (
                        execution.repo_url.replace("https://github.com/", "").removesuffix(".git")
                        if execution and execution.repo_url
                        else None
                    )
                    if repo:
                        # Check if a PR already exists for this branch
                        pr = await tracker.get_pr_by_branch(repo, artifacts.branch)
                        if pr:
                            artifacts.pr_number = pr["number"]
                            artifacts.pr_url = pr.get("html_url")
                            logger.info(
                                f"Found existing PR #{artifacts.pr_number} on "
                                f"{artifacts.branch} for execution {execution_id}"
                            )
                        else:
                            # Create PR as bot if mode produces PRs
                            mode = execution.mode if execution else None
                            if mode in ("implement", "retry_with_feedback"):
                                await _create_pr(
                                    tracker, repo, issue_id, artifacts, execution_id
                                )
            except Exception as e:
                logger.warning(f"PR detection/creation failed for {execution_id}: {e}")

        # Persist cost
        if artifacts.cost_usd and artifacts.cost_usd > 0:
            try:
                await db.set_cost(execution_id, int(artifacts.cost_usd * 100))
            except Exception as e:
                logger.warning(f"Failed to persist cost for {execution_id}: {e}")

        # Store session info in metadata
        if artifacts.session_s3_key:
            try:
                issue_id = await db.get_issue_id_for_execution(execution_id)
                if issue_id:
                    execution = await db.get_execution(execution_id)
                    repo = (
                        execution.repo_url.replace("https://github.com/", "").removesuffix(".git")
                        if execution and execution.repo_url
                        else None
                    )
                    if repo:
                        await db.merge_issue_metadata(
                            issue_number=int(issue_id),
                            repo=repo,
                            metadata_update={
                                "session_s3_key": artifacts.session_s3_key,
                                "session_id": artifacts.session_id,
                            },
                        )
            except Exception as e:
                logger.warning(f"Failed to store session metadata for {execution_id}: {e}")

        # Update DB execution record
        try:
            await db.update_execution_result(
                execution_id=execution_id,
                status=ExecutionStatus.COMPLETED,
                result=artifacts.result,
                pr_number=artifacts.pr_number,
                branch=artifacts.branch,
            )
        except Exception as e:
            logger.warning(f"Failed to update DB for {execution_id}: {e}")

        return artifacts

    async def on_execution_failed(execution_id: UUID, error_msg: str) -> None:
        """Handle failed execution — update DB."""
        try:
            await db.update_execution_result(
                execution_id=execution_id,
                status=ExecutionStatus.FAILED,
                result=error_msg,
            )
        except Exception as e:
            logger.warning(f"Failed to update DB for {execution_id}: {e}")

    async def _create_pr(tracker_ref, repo, issue_id, artifacts, execution_id):
        """Create a PR using the App installation token (bot account)."""
        try:
            issue = await tracker_ref.get_issue(repo, issue_id)
            reviewer = issue.author or None
            title = f"Fix #{issue_id}: {issue.title}" if issue.title else f"Fix #{issue_id}"
            if len(title) > 256:
                title = title[:253] + "..."
            body = f"Closes #{issue_id}"

            pr_data = await tracker_ref.create_pr(
                repo=repo,
                title=title,
                body=body,
                head=artifacts.branch,
                labels=["ag/review-pending"],
                reviewers=[reviewer] if reviewer else None,
            )
            if pr_data:
                artifacts.pr_number = pr_data["number"]
                artifacts.pr_url = pr_data.get("html_url")
                logger.info(
                    f"Created PR #{artifacts.pr_number} on {artifacts.branch} "
                    f"for execution {execution_id} (as bot)"
                )
        except Exception as e:
            logger.warning(f"Failed to create PR for execution {execution_id}: {e}")

    return ClaudeCodeCallbacks(
        on_execution_completed=on_execution_completed,
        on_execution_failed=on_execution_failed,
    )
