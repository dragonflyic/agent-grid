"""Oz execution grid callbacks — wired during application startup.

Contains the coordinator-layer logic that was previously inlined in oz_grid.py.
This keeps the execution_grid layer free of coordinator/issue_tracker imports.
"""

import logging
from uuid import UUID

from ..execution_grid.oz_grid import OzCallbacks, RunArtifacts
from ..execution_grid.public_api import ExecutionStatus

logger = logging.getLogger("agent_grid.oz_callbacks")


def build_oz_callbacks(db, tracker) -> OzCallbacks:
    """Build OzCallbacks wired to the given database and issue tracker."""

    async def on_run_created(execution_id: UUID, oz_run_id: str, session_link: str | None) -> None:
        await db.set_external_run_id(execution_id, oz_run_id)
        if session_link:
            await db.set_session_link(execution_id, session_link)

    async def recover_runs() -> list[tuple[UUID, str]]:
        return await db.get_active_executions_with_external_run_id()

    async def on_run_succeeded(execution_id: UUID, artifacts: RunArtifacts) -> RunArtifacts:
        # Create or detect PR for modes that produce branches
        if artifacts.pr_number is None:
            try:
                issue_id = await db.get_issue_id_for_execution(execution_id)
                if issue_id:
                    execution = await db.get_execution(execution_id)
                    repo = (
                        execution.repo_url.replace("https://github.com/", "").rstrip(".git")
                        if execution and execution.repo_url
                        else None
                    )
                    if repo:
                        # Determine candidate branches based on mode
                        candidates = (
                            f"agent/{issue_id}",
                            f"agent/{issue_id}-retry",
                        )

                        for candidate_branch in candidates:
                            # Check if a PR already exists for this branch
                            pr = await tracker.get_pr_by_branch(repo, candidate_branch)
                            if pr:
                                artifacts.pr_number = pr["number"]
                                artifacts.branch = candidate_branch
                                artifacts.pr_url = pr.get("html_url")
                                logger.info(
                                    f"Found existing PR #{artifacts.pr_number} on "
                                    f"{artifacts.branch} for execution {execution_id}"
                                )
                                break

                        # If no PR exists and mode creates PRs, create one
                        # via the App installation token (shows as bot account)
                        mode = execution.mode if execution else None
                        if (
                            artifacts.pr_number is None
                            and mode in ("implement", "retry_with_feedback")
                            and hasattr(tracker, "create_pr")
                        ):
                            await _create_pr_for_execution(
                                tracker, repo, issue_id, candidates, artifacts,
                                execution_id,
                            )
            except Exception as e:
                logger.warning(f"PR detection/creation failed for {execution_id}: {e}")

        # Persist cost
        if artifacts.cost_cents and artifacts.cost_cents > 0:
            try:
                await db.set_cost(execution_id, artifacts.cost_cents)
            except Exception as e:
                logger.warning(f"Failed to persist cost for {execution_id}: {e}")

        # Update DB
        try:
            await db.update_execution_result(
                execution_id=execution_id,
                status=ExecutionStatus.COMPLETED,
                result=artifacts.result,
                pr_number=artifacts.pr_number,
                branch=artifacts.branch,
            )
        except Exception as e:
            logger.warning(f"Failed to update DB for execution {execution_id}: {e}")

        return artifacts

    async def on_run_failed(execution_id: UUID, error_msg: str) -> None:
        try:
            await db.update_execution_result(
                execution_id=execution_id,
                status=ExecutionStatus.FAILED,
                result=error_msg,
            )
        except Exception as e:
            logger.warning(f"Failed to update DB for execution {execution_id}: {e}")

    async def _create_pr_for_execution(
        tracker_ref, repo: str, issue_id: str,
        candidate_branches: tuple, artifacts: RunArtifacts,
        execution_id: UUID,
    ) -> None:
        """Create a PR using the App installation token (shows as bot account)."""
        try:
            issue = await tracker_ref.get_issue(repo, issue_id)
            reviewer = issue.author or None
            title = f"Fix #{issue_id}: {issue.title}" if issue.title else f"Fix #{issue_id}"
            # Trim title to 256 chars (GitHub limit)
            if len(title) > 256:
                title = title[:253] + "..."
            body = f"Closes #{issue_id}"

            for branch in candidate_branches:
                pr_data = await tracker_ref.create_pr(
                    repo=repo,
                    title=title,
                    body=body,
                    head=branch,
                    labels=["ag/review-pending"],
                    reviewers=[reviewer] if reviewer else None,
                )
                if pr_data:
                    artifacts.pr_number = pr_data["number"]
                    artifacts.branch = branch
                    artifacts.pr_url = pr_data.get("html_url")
                    logger.info(
                        f"Created PR #{artifacts.pr_number} on {branch} "
                        f"for execution {execution_id} (as bot)"
                    )
                    break
        except Exception as e:
            logger.warning(f"Failed to create PR for execution {execution_id}: {e}")

    return OzCallbacks(
        on_run_created=on_run_created,
        recover_runs=recover_runs,
        on_run_succeeded=on_run_succeeded,
        on_run_failed=on_run_failed,
    )
