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
from uuid import uuid4

from ..config import settings
from ..execution_grid import AgentExecution, ExecutionConfig, ExecutionStatus, get_execution_grid, utc_now
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.label_manager import get_label_manager
from ..issue_tracker.metadata import embed_metadata
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
            elif classification.category == "BLOCKED":
                await labels.transition_to(repo, issue.id, "ag/blocked")
                question = classification.blocking_question or classification.reason
                comment = embed_metadata(
                    f"**Agent needs clarification:**\n\n{question}",
                    {"type": "blocked", "reason": classification.reason},
                )
                await self._tracker.add_comment(repo, issue.id, comment)
                logger.info(f"Issue #{issue.number}: BLOCKED — posted question")
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

    async def _claim_and_launch(
        self,
        issue_id: str,
        repo_url: str,
        prompt: str,
        mode: str = "implement",
        issue_number: int | None = None,
        context: dict | None = None,
    ) -> bool:
        """Atomically claim an issue and launch the agent.

        Claims the DB row FIRST to prevent races, then launches the Fly machine.
        If launch fails, the execution is marked as FAILED.

        Returns True if the agent was launched, False if claim failed.
        """
        execution_id = uuid4()
        execution = AgentExecution(
            id=execution_id,
            repo_url=repo_url,
            status=ExecutionStatus.PENDING,
            prompt=prompt,
            started_at=utc_now(),
        )

        claimed = await self._db.try_claim_issue(execution, issue_id=issue_id)
        if not claimed:
            logger.info(f"Issue #{issue_id}: already has active execution, skipping")
            return False

        config = ExecutionConfig(repo_url=repo_url, prompt=prompt)
        grid = get_execution_grid()
        try:
            if hasattr(grid, "launch_agent") and "mode" in grid.launch_agent.__code__.co_varnames:
                kwargs: dict = {"mode": mode, "execution_id": execution_id}
                if issue_number is not None:
                    kwargs["issue_number"] = issue_number
                if context is not None:
                    kwargs["context"] = context
                await grid.launch_agent(config, **kwargs)
            else:
                await grid.launch_agent(config)
        except Exception as e:
            logger.error(f"Failed to launch agent for issue #{issue_id}: {e}")
            execution.status = ExecutionStatus.FAILED
            execution.result = f"Launch failed: {e}"
            await self._db.update_execution(execution)
            return False

        return True

    async def _has_active_execution(self, issue_id: str) -> bool:
        """Check if there's already a running/pending execution for this issue."""
        existing = await self._db.get_execution_for_issue(issue_id)
        if existing and existing.status in (ExecutionStatus.PENDING, ExecutionStatus.RUNNING):
            logger.info(f"Issue #{issue_id}: already has active execution {existing.id}, skipping")
            return True
        return False

    async def _launch_simple(self, repo: str, issue) -> None:
        """Launch an agent for a SIMPLE issue."""
        if await self._has_active_execution(issue.id):
            return

        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ag/in-progress")

        prompt = build_prompt(issue, repo, mode="implement")
        repo_url = f"https://github.com/{repo}.git"

        launched = await self._claim_and_launch(
            issue_id=issue.id,
            repo_url=repo_url,
            prompt=prompt,
            mode="implement",
            issue_number=issue.number,
        )
        if launched:
            logger.info(f"Issue #{issue.number}: SIMPLE — launched agent")

    async def _launch_unblocked(self, repo: str, issue) -> None:
        """Launch an agent for a previously-blocked issue that got a human reply."""
        if await self._has_active_execution(issue.id):
            return

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

        launched = await self._claim_and_launch(
            issue_id=issue.id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="implement",
            issue_number=issue.number,
        )
        if launched:
            logger.info(f"Issue #{issue.number}: UNBLOCKED — launched agent")

    async def _launch_planner(self, repo: str, issue) -> None:
        """Launch an agent to decompose a COMPLEX issue into sub-issues."""
        if await self._has_active_execution(issue.id):
            return

        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ag/planning")

        prompt = build_prompt(issue, repo, mode="plan")

        launched = await self._claim_and_launch(
            issue_id=issue.id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="plan",
            issue_number=issue.number,
        )
        if launched:
            logger.info(f"Issue #{issue.number}: COMPLEX — launched planner agent")

    async def _launch_review_handler(self, repo: str, pr_info: dict) -> None:
        """Launch an agent to address PR review comments."""
        issue_id = pr_info["issue_id"]
        if await self._has_active_execution(issue_id):
            return

        issue = await self._tracker.get_issue(repo, issue_id)
        checkpoint = await self._db.get_latest_checkpoint(issue_id)

        context = {
            "pr_number": pr_info["pr_number"],
            "existing_branch": pr_info["branch"],
            "review_comments": pr_info["review_comments"],
        }

        prompt = build_prompt(issue, repo, mode="address_review", context=context, checkpoint=checkpoint)

        launched = await self._claim_and_launch(
            issue_id=issue_id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="address_review",
            issue_number=int(issue_id),
            context=context,
        )
        if launched:
            logger.info(f"PR #{pr_info['pr_number']}: launched review handler agent")

    async def _launch_retry(self, repo: str, pr_info: dict) -> None:
        """Launch a retry agent for a closed PR with feedback."""
        issue_id = pr_info["issue_id"]
        if await self._has_active_execution(issue_id):
            return

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

        labels = get_label_manager()
        await labels.transition_to(repo, issue_id, "ag/in-progress")

        launched = await self._claim_and_launch(
            issue_id=issue_id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="retry_with_feedback",
            issue_number=int(issue_id),
            context=context,
        )
        if launched:
            # Increment retry count only on successful launch
            await self._db.upsert_issue_state(
                issue_number=int(issue_id),
                repo=repo,
                retry_count=retry_count + 1,
            )
            logger.info(f"Issue #{issue_id}: retry #{retry_count + 1} — launched agent")

    async def _launch_ci_fix(self, repo: str, check_info: dict) -> bool:
        """Launch an agent to fix a failing CI check on an agent PR.

        Returns True if agent was launched, False otherwise.
        """
        import re

        branch = check_info.get("branch", "")
        match = re.match(r"agent/(\d+)(?:-|$)", branch)
        if not match:
            return False
        issue_id = match.group(1)

        if await self._has_active_execution(issue_id):
            return False

        issue = await self._tracker.get_issue(repo, issue_id)
        checkpoint = await self._db.get_latest_checkpoint(issue_id)

        context = {
            "existing_branch": branch,
            "pr_number": check_info.get("pr_number"),
            "check_name": check_info.get("check_name", ""),
            "check_output": check_info.get("check_output", ""),
            "check_url": check_info.get("check_url", ""),
        }

        prompt = build_prompt(issue, repo, mode="fix_ci", context=context, checkpoint=checkpoint)

        launched = await self._claim_and_launch(
            issue_id=issue_id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="fix_ci",
            issue_number=int(issue_id),
            context=context,
        )
        if launched:
            logger.info(f"Issue #{issue_id}: launched CI fix agent for '{check_info.get('check_name')}'")
        return launched

    async def _check_in_progress(self, repo: str) -> None:
        """Phase 4: Check in-progress executions for timeouts."""
        from ..execution_grid import ExecutionStatus

        running = await self._db.get_running_executions()
        # Also check pending executions that may have stalled
        pending = await self._db.list_executions(status=ExecutionStatus.PENDING)

        for execution in running + pending:
            ref_time = execution.started_at or execution.created_at
            if not ref_time:
                continue
            now = utc_now()
            elapsed = (now - ref_time).total_seconds()
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
