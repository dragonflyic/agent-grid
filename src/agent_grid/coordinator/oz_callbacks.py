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
        # Fallback PR detection when Oz didn't provide a PR artifact
        if artifacts.pr_number is None:
            try:
                issue_id = await db.get_issue_id_for_execution(execution_id)
                if issue_id:
                    # Get repo from the execution record
                    execution = await db.get_execution(execution_id)
                    repo = (
                        execution.repo_url.replace("https://github.com/", "").rstrip(".git")
                        if execution and execution.repo_url
                        else None
                    )
                    if repo:
                        for candidate_branch in (
                            f"agent/{issue_id}",
                            f"agent/{issue_id}-retry",
                        ):
                            pr = await tracker.get_pr_by_branch(repo, candidate_branch)
                            if pr:
                                artifacts.pr_number = pr["number"]
                                artifacts.branch = candidate_branch
                                artifacts.pr_url = pr.get("html_url")
                                logger.info(
                                    f"Fallback: found PR #{artifacts.pr_number} on branch "
                                    f"{artifacts.branch} for execution {execution_id}"
                                )
                                break
            except Exception as e:
                logger.warning(f"Fallback PR detection failed for {execution_id}: {e}")

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

    return OzCallbacks(
        on_run_created=on_run_created,
        recover_runs=recover_runs,
        on_run_succeeded=on_run_succeeded,
        on_run_failed=on_run_failed,
    )
