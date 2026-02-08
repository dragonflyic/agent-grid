"""The Tech Lead's main cron loop.

Runs every N seconds (default 1 hour). Each cycle performs 7 phases:
1. Scan — fetch unprocessed open issues
2. Classify — SIMPLE/COMPLEX/BLOCKED/SKIP
3. Act — spawn agents, create sub-issues, post questions
4. Monitor in-progress — check agent statuses
5. Monitor PRs — detect human review comments
6. Monitor closed PRs — detect feedback on closed PRs
7. Resolve blockers — unblock issues with human responses
"""

import asyncio
import logging

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.label_manager import get_label_manager
from .blocker_resolver import get_blocker_resolver
from .budget_manager import get_budget_manager
from .classifier import get_classifier
from .database import get_database
from .dependency_resolver import get_dependency_resolver
from .pr_monitor import get_pr_monitor
from .prompt_builder import build_prompt
from .scanner import get_scanner

logger = logging.getLogger("agent_grid.cron")


class ManagementLoop:
    def __init__(self, interval_seconds: int | None = None):
        self._interval = interval_seconds or settings.management_loop_interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._db = get_database()
        self._tracker = get_issue_tracker()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Management loop started (interval={self._interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        # Run first cycle after a short delay (let other services start)
        await asyncio.sleep(10)
        while self._running:
            try:
                await self.run_cycle()
            except Exception:
                logger.exception("Error in management loop cycle")
            await asyncio.sleep(self._interval)

    async def run_cycle(self) -> None:
        """Run one full cycle of all 7 phases."""
        repo = settings.target_repo
        if not repo:
            logger.warning("No target_repo configured, skipping cycle")
            return

        logger.info(f"=== Starting cron cycle for {repo} ===")

        # Phase 1: Scan
        scanner = get_scanner()
        candidates = await scanner.scan(repo)
        logger.info(f"Phase 1: Found {len(candidates)} candidate issues")

        # Phase 2 + 3: Classify and act
        classifier = get_classifier()
        budget = get_budget_manager()
        labels = get_label_manager()

        for issue in candidates:
            can_launch, reason = await budget.can_launch_agent()
            if not can_launch:
                logger.info(f"Budget limit reached: {reason}. Stopping new assignments.")
                break

            classification = await classifier.classify(issue)

            # Save classification to DB
            await self._db.upsert_issue_state(
                issue_number=issue.number,
                repo=repo,
                classification=classification.category,
            )

            if classification.category == "SIMPLE":
                await self._launch_simple(repo, issue)
            elif classification.category == "COMPLEX":
                await self._launch_planner(repo, issue)
            elif classification.category == "SKIP":
                await labels.transition_to(repo, issue.id, "ag/skipped")
                await self._tracker.add_comment(
                    repo,
                    issue.id,
                    f"Skipping automated work: {classification.reason}",
                )
                logger.info(f"Issue #{issue.number}: SKIPPED — {classification.reason}")

        # Phase 4: Monitor in-progress
        await self._check_in_progress(repo)

        # Phase 5: Monitor PRs for review comments
        pr_monitor = get_pr_monitor()
        prs_needing_work = await pr_monitor.check_prs(repo)
        for pr_info in prs_needing_work:
            if pr_info["issue_id"]:
                await self._launch_review_handler(repo, pr_info)

        # Phase 6: Monitor closed PRs with feedback
        closed_prs = await pr_monitor.check_closed_prs(repo)
        for pr_info in closed_prs:
            if pr_info["issue_id"]:
                await self._launch_retry(repo, pr_info)

        # Phase 7: Resolve blockers — launch agents directly for unblocked issues
        blocker_resolver = get_blocker_resolver()
        unblocked = await blocker_resolver.check_blocked_issues(repo)
        for issue in unblocked:
            await self._launch_unblocked(repo, issue)
        if unblocked:
            logger.info(f"Phase 7: Launched {len(unblocked)} unblocked issues")

        # Bonus: Check dependency resolution
        dep_resolver = get_dependency_resolver()
        await dep_resolver.check_dependencies(repo)
        await dep_resolver.check_parent_completion(repo)

        logger.info("=== Cron cycle complete ===")

    async def _launch_simple(self, repo: str, issue) -> None:
        """Launch an agent for a SIMPLE issue."""
        from ..execution_grid import ExecutionConfig, get_execution_grid

        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ag/in-progress")

        prompt = build_prompt(issue, repo, mode="implement")
        config = ExecutionConfig(
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
        )

        grid = get_execution_grid()
        # Use the extended launch_agent if Fly grid (supports mode/issue_number)
        if hasattr(grid, "launch_agent") and "mode" in grid.launch_agent.__code__.co_varnames:
            execution_id = await grid.launch_agent(
                config,
                mode="implement",
                issue_number=issue.number,
            )
        else:
            execution_id = await grid.launch_agent(config)

        # Record in DB
        from ..execution_grid import AgentExecution, ExecutionStatus

        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=prompt,
        )
        await self._db.create_execution(execution, issue_id=issue.id)
        logger.info(f"Issue #{issue.number}: SIMPLE — launched agent {execution_id}")

    async def _launch_unblocked(self, repo: str, issue) -> None:
        """Launch an agent for a previously-blocked issue that got a human reply."""
        from ..execution_grid import ExecutionConfig, get_execution_grid
        from ..issue_tracker.metadata import extract_metadata

        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ag/in-progress")

        # Extract human replies after the last blocking comment
        clarification_comments = []
        last_block_idx = None
        for i, comment in enumerate(issue.comments):
            meta = extract_metadata(comment.body)
            if meta and meta.get("type") == "blocked":
                last_block_idx = i

        if last_block_idx is not None:
            for comment in issue.comments[last_block_idx + 1 :]:
                if extract_metadata(comment.body) is None:
                    clarification_comments.append(comment.body)

        context = {"clarification_comments": clarification_comments}
        prompt = build_prompt(issue, repo, mode="implement", context=context)
        config = ExecutionConfig(
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
        )

        grid = get_execution_grid()
        if hasattr(grid, "launch_agent") and "mode" in grid.launch_agent.__code__.co_varnames:
            execution_id = await grid.launch_agent(
                config,
                mode="implement",
                issue_number=issue.number,
            )
        else:
            execution_id = await grid.launch_agent(config)

        from ..execution_grid import AgentExecution, ExecutionStatus

        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=prompt,
        )
        await self._db.create_execution(execution, issue_id=issue.id)
        logger.info(f"Issue #{issue.number}: UNBLOCKED — launched agent {execution_id}")

    async def _launch_planner(self, repo: str, issue) -> None:
        """Launch an agent to decompose a COMPLEX issue into sub-issues."""
        from ..execution_grid import ExecutionConfig, get_execution_grid

        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ag/planning")

        prompt = build_prompt(issue, repo, mode="plan")
        config = ExecutionConfig(
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
        )

        grid = get_execution_grid()
        if hasattr(grid, "launch_agent") and "mode" in grid.launch_agent.__code__.co_varnames:
            execution_id = await grid.launch_agent(
                config,
                mode="plan",
                issue_number=issue.number,
            )
        else:
            execution_id = await grid.launch_agent(config)

        from ..execution_grid import AgentExecution, ExecutionStatus

        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=prompt,
        )
        await self._db.create_execution(execution, issue_id=issue.id)
        logger.info(f"Issue #{issue.number}: COMPLEX — launched planner agent {execution_id}")

    async def _launch_review_handler(self, repo: str, pr_info: dict) -> None:
        """Launch an agent to address PR review comments."""
        from ..execution_grid import ExecutionConfig, get_execution_grid

        issue_id = pr_info["issue_id"]
        issue = await self._tracker.get_issue(repo, issue_id)

        checkpoint = await self._db.get_latest_checkpoint(issue_id)

        context = {
            "pr_number": pr_info["pr_number"],
            "existing_branch": pr_info["branch"],
            "review_comments": pr_info["review_comments"],
        }

        prompt = build_prompt(issue, repo, mode="address_review", context=context, checkpoint=checkpoint)
        config = ExecutionConfig(
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
        )

        grid = get_execution_grid()
        if hasattr(grid, "launch_agent") and "mode" in grid.launch_agent.__code__.co_varnames:
            execution_id = await grid.launch_agent(
                config,
                mode="address_review",
                issue_number=int(issue_id),
                context=context,
            )
        else:
            execution_id = await grid.launch_agent(config)

        from ..execution_grid import AgentExecution

        execution = AgentExecution(id=execution_id, repo_url=config.repo_url, prompt=prompt)
        await self._db.create_execution(execution, issue_id=issue_id)
        logger.info(f"PR #{pr_info['pr_number']}: launched review handler agent {execution_id}")

    async def _launch_retry(self, repo: str, pr_info: dict) -> None:
        """Launch a retry agent for a closed PR with feedback."""
        from ..execution_grid import ExecutionConfig, get_execution_grid

        issue_id = pr_info["issue_id"]
        issue = await self._tracker.get_issue(repo, issue_id)

        checkpoint = await self._db.get_latest_checkpoint(issue_id)

        # Check retry count
        issue_state = await self._db.get_issue_state(int(issue_id), repo)
        retry_count = (issue_state or {}).get("retry_count", 0)
        if retry_count >= settings.max_retries_per_issue:
            labels = get_label_manager()
            await labels.transition_to(repo, issue_id, "ag/failed")
            await self._tracker.add_comment(
                repo,
                issue_id,
                f"Max retries ({settings.max_retries_per_issue}) reached. Needs human intervention.",
            )
            return

        context = {
            "closed_pr_number": pr_info["pr_number"],
            "human_feedback": pr_info["human_feedback"],
            "what_not_to_do": checkpoint.get("context_summary", "") if checkpoint else "",
        }

        prompt = build_prompt(issue, repo, mode="retry_with_feedback", context=context, checkpoint=checkpoint)
        config = ExecutionConfig(repo_url=f"https://github.com/{repo}.git", prompt=prompt)

        grid = get_execution_grid()
        if hasattr(grid, "launch_agent") and "mode" in grid.launch_agent.__code__.co_varnames:
            execution_id = await grid.launch_agent(
                config,
                mode="retry_with_feedback",
                issue_number=int(issue_id),
                context=context,
            )
        else:
            execution_id = await grid.launch_agent(config)

        # Increment retry count
        await self._db.upsert_issue_state(
            issue_number=int(issue_id),
            repo=repo,
            retry_count=retry_count + 1,
        )

        labels = get_label_manager()
        await labels.transition_to(repo, issue_id, "ag/in-progress")

        from ..execution_grid import AgentExecution

        execution = AgentExecution(id=execution_id, repo_url=config.repo_url, prompt=prompt)
        await self._db.create_execution(execution, issue_id=issue_id)
        logger.info(f"Issue #{issue_id}: retry #{retry_count + 1} — launched agent {execution_id}")

    async def _check_in_progress(self, repo: str) -> None:
        """Phase 4: Check in-progress executions for timeouts."""
        from ..execution_grid import ExecutionStatus

        running = await self._db.get_running_executions()

        for execution in running:
            if execution.started_at:
                from datetime import datetime, timezone

                elapsed = (datetime.now(timezone.utc) - execution.started_at).total_seconds()
                if elapsed > settings.execution_timeout_seconds:
                    logger.warning(f"Execution {execution.id} timed out after {elapsed:.0f}s")
                    execution.status = ExecutionStatus.FAILED
                    execution.result = "Timed out"
                    await self._db.update_execution(execution)

    async def run_once(self) -> None:
        """Run a single cycle (for testing)."""
        await self.run_cycle()


# Global instance
_management_loop: ManagementLoop | None = None


def get_management_loop() -> ManagementLoop:
    global _management_loop
    if _management_loop is None:
        _management_loop = ManagementLoop()
    return _management_loop
