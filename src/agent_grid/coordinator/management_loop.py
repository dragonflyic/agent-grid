"""The Tech Lead's main cron loop.

Runs every N seconds (default 1 hour). Each cycle performs 10 phases:
1. Scan — fetch unprocessed open issues
2. Classify — SIMPLE/COMPLEX/BLOCKED/SKIP
3. Act — spawn agents, create sub-issues, post questions
4. Monitor in-progress — check agent statuses
   4b. Reap stale in-progress issues
   4c. Auto-retry failed issues (retry_count < max_retries)
5. Monitor PRs — detect human review comments
6. Monitor closed PRs — detect feedback on closed PRs
7. Poll CI failures — backup to webhook delivery
8. Resolve blockers — unblock issues with human responses
9. Proactive scan — find unlabeled issues suitable for automation
"""

import asyncio
import json
import logging
import re

from ..config import settings
from ..execution_grid import get_execution_grid, utc_now
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.label_manager import get_label_manager
from .agent_launcher import get_agent_launcher
from .blocker_resolver import get_blocker_resolver
from .budget_manager import get_budget_manager
from .classifier import get_classifier
from .database import get_database
from .dependency_resolver import get_dependency_resolver
from .pr_monitor import get_pr_monitor
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
        """Run one full cycle of all 9 phases."""
        repo = settings.target_repo
        if not repo:
            logger.warning("No target_repo configured, skipping cycle")
            return

        logger.info(f"=== Starting cron cycle for {repo} ===")
        launcher = get_agent_launcher()

        # Phase 1: Scan
        scanner = get_scanner()
        candidates = await scanner.scan(repo)
        logger.info(f"Phase 1: Found {len(candidates)} candidate issues")

        # Phase 2: Sanity check and launch scouts
        classifier = get_classifier()
        budget = get_budget_manager()
        labels = get_label_manager()

        for issue in candidates:
            can_launch, reason = await budget.can_launch_agent()
            if not can_launch:
                logger.info(f"Budget limit reached: {reason}. Stopping new assignments.")
                await self._db.record_pipeline_event(issue.number, repo, "budget_blocked", "launch", {"reason": reason})
                break

            sanity = await classifier.sanity_check(issue)

            await self._db.upsert_issue_state(
                issue_number=issue.number,
                repo=repo,
                classification=sanity.verdict,
            )
            await self._db.record_pipeline_event(
                issue.number, repo, "sanity_check", "classify",
                {"verdict": sanity.verdict, "reason": sanity.reason},
            )

            if sanity.verdict == "SKIP":
                await labels.transition_to(repo, issue.id, "ag/skipped")
                await self._tracker.add_comment(
                    repo, issue.id,
                    f"Skipping: {sanity.reason}",
                )
                logger.info(f"Issue #{issue.number}: SKIPPED — {sanity.reason}")
                continue

            # Launch scout agent
            await launcher.launch_scout(repo, issue)

        # Phase 4: Monitor in-progress
        await self._check_in_progress(repo)
        await self._reap_stale_in_progress(repo)

        # Phase 4c: Auto-retry failed issues
        await self._auto_retry_failed(repo)

        # Phase 5: Monitor PRs for review comments
        pr_monitor = get_pr_monitor()
        prs_raw = await pr_monitor.check_prs(repo)
        seen_pr_issues: dict[str, dict] = {}
        for pr_info in prs_raw:
            iid = pr_info.get("issue_id")
            if not iid:
                continue
            if iid in seen_pr_issues:
                extra = pr_info.get("review_comments", "")
                if extra and extra not in seen_pr_issues[iid].get("review_comments", ""):
                    seen_pr_issues[iid]["review_comments"] += "\n\n---\n\n" + extra
            else:
                seen_pr_issues[iid] = dict(pr_info)
        for pr_info in seen_pr_issues.values():
            await launcher.launch_review_handler(repo, pr_info)

        # Phase 5b: Check for merge conflicts on agent PRs
        await self._check_merge_conflicts(repo, launcher)

        # Phase 6: Monitor closed PRs with feedback
        closed_prs = await pr_monitor.check_closed_prs(repo)
        for pr_info in closed_prs:
            if pr_info["issue_id"]:
                await launcher.launch_retry(repo, pr_info)

        # Phase 7: Poll for CI failures (backup to webhook delivery)
        from .ci_monitor import get_ci_monitor

        ci_monitor = get_ci_monitor()
        ci_failures = await ci_monitor.check_ci_failures(repo)
        ci_launched = 0
        for check_info in ci_failures:
            issue_match = re.match(r"agent/(\d+)(?:-|$)", check_info.get("branch", ""))
            if not issue_match:
                continue
            ci_issue_id = issue_match.group(1)

            ci_state = await self._db.get_issue_state(int(ci_issue_id), repo)
            ci_meta = (ci_state or {}).get("metadata") or {}
            if isinstance(ci_meta, str):
                ci_meta = json.loads(ci_meta)
            ci_fix_count = ci_meta.get("ci_fix_count", 0)
            if ci_fix_count >= settings.max_ci_fix_retries:
                logger.warning(
                    f"Issue #{ci_issue_id}: CI fix retry limit ({settings.max_ci_fix_retries}) reached via polling"
                )
                continue

            check_info = await launcher.enrich_check_output(repo, check_info)
            launched = await launcher.launch_ci_fix(repo, check_info)
            if launched:
                await self._db.merge_issue_metadata(
                    issue_number=int(ci_issue_id),
                    repo=repo,
                    metadata_update={
                        "last_ci_check_sha": check_info.get("head_sha", ""),
                        "ci_fix_count": ci_fix_count + 1,
                    },
                )
                ci_launched += 1
        if ci_launched:
            logger.info(f"Phase 7: Launched {ci_launched} CI fix agents")

        # Phase 8: Resolve blockers
        blocker_resolver = get_blocker_resolver()
        unblocked = await blocker_resolver.check_blocked_issues(repo)
        for issue in unblocked:
            await launcher.launch_unblocked(repo, issue)
        if unblocked:
            logger.info(f"Phase 8: Launched {len(unblocked)} unblocked issues")

        dep_resolver = get_dependency_resolver()
        await dep_resolver.check_dependencies(repo)
        await dep_resolver.check_parent_completion(repo)

        # Phase 9: Proactive scan
        if settings.proactive_scan_enabled:
            await self._maybe_run_proactive_scan(repo)

        logger.info("=== Cron cycle complete ===")

    async def _maybe_run_proactive_scan(self, repo: str) -> None:
        """Phase 8: Proactive scan — find unlabeled issues suitable for automation.

        Only runs every N cycles (configured by proactive_scan_every_n_cycles).
        Uses cron_state to track the cycle count.
        """
        from .proactive_scanner import get_proactive_scanner

        # Check if it's time to run
        cron_state = await self._db.get_cron_state("proactive_scan") or {}
        cycle_count = cron_state.get("cycle_count", 0) + 1

        if cycle_count < settings.proactive_scan_every_n_cycles:
            await self._db.set_cron_state("proactive_scan", {"cycle_count": cycle_count})
            return

        # Reset cycle count
        await self._db.set_cron_state(
            "proactive_scan",
            {
                "cycle_count": 0,
                "last_run_at": utc_now().isoformat(),
            },
        )

        logger.info("Phase 9: Running proactive scan")

        proactive_scanner = get_proactive_scanner()
        classifier = get_classifier()
        budget = get_budget_manager()
        labels = get_label_manager()
        launcher = get_agent_launcher()

        candidates = await proactive_scanner.scan(repo)
        picked_up = 0

        for issue in candidates:
            if picked_up >= settings.proactive_max_per_cycle:
                break

            can_launch, reason = await budget.can_launch_agent()
            if not can_launch:
                logger.info(f"Proactive scan: budget limit reached: {reason}")
                break

            # Sanity check
            sanity = await classifier.sanity_check(issue)

            await self._db.upsert_issue_state(
                issue_number=issue.number,
                repo=repo,
                classification=sanity.verdict,
            )

            if sanity.verdict == "SKIP":
                await self._db.merge_issue_metadata(
                    issue_number=issue.number,
                    repo=repo,
                    metadata_update={"proactive_skipped": True},
                )
                continue

            # Launch scout agent
            await labels.add_label(repo, issue.id, "ag/proactive")

            owner_tag = f"@{issue.author}" if issue.author else "the issue author"
            await self._tracker.add_comment(
                repo,
                issue.id,
                f"I noticed this issue and I'm confident I can handle it. "
                f"Starting work now — {owner_tag}, I'll tag you for review "
                f"when the PR is ready.",
            )

            await self._db.merge_issue_metadata(
                issue_number=issue.number,
                repo=repo,
                metadata_update={"proactive_picked": True},
            )

            await launcher.launch_scout(repo, issue)

            picked_up += 1

        logger.info(f"Phase 9: Proactive scan complete — picked up {picked_up} issues")

    async def _check_in_progress(self, repo: str) -> None:
        """Phase 4: Check in-progress executions for timeouts."""
        from ..execution_grid import ExecutionStatus

        running = await self._db.get_running_executions()
        # Also check pending executions that may have stalled
        pending = await self._db.list_executions(status=ExecutionStatus.PENDING)

        grid = get_execution_grid()

        for execution in running + pending:
            ref_time = execution.started_at or execution.created_at
            if not ref_time:
                continue
            now = utc_now()
            elapsed = (now - ref_time).total_seconds()
            if elapsed > settings.execution_timeout_seconds:
                logger.warning(f"Execution {execution.id} timed out after {elapsed:.0f}s")
                # Cancel the actual run (Oz/Fly) so it stops burning compute
                try:
                    await grid.cancel_execution(execution.id)
                except Exception as e:
                    logger.warning(f"Failed to cancel backend execution {execution.id}: {e}")
                execution.status = ExecutionStatus.FAILED
                execution.result = "Timed out"
                await self._db.update_execution(execution)
                # Transition label so the issue exits in-progress
                issue_id = await self._db.get_issue_id_for_execution(execution.id)
                if issue_id:
                    labels = get_label_manager()
                    try:
                        await labels.transition_to(repo, issue_id, "ag/failed")
                    except Exception as e:
                        logger.warning(f"Failed to transition issue #{issue_id} label: {e}")
                    await self._db.record_pipeline_event(
                        issue_number=int(issue_id),
                        repo=repo,
                        event_type="execution_timeout",
                        stage="monitor",
                        detail={"execution_id": str(execution.id), "elapsed_seconds": int(elapsed)},
                    )
                    logger.info(f"Issue #{issue_id}: timed out — transitioned to ag/failed")

    async def _reap_stale_in_progress(self, repo: str) -> None:
        """Phase 4b: Reap ag/in-progress issues with no active execution.

        Catches issues where the execution finished but the label was never
        transitioned (e.g., lost callbacks, bugs in older code).
        """
        from ..execution_grid import ExecutionStatus
        from ..issue_tracker.public_api import IssueStatus

        tracker = get_issue_tracker()
        all_open = await tracker.list_issues(repo, status=IssueStatus.OPEN)
        in_progress = [i for i in all_open if "ag/in-progress" in i.labels]

        if not in_progress:
            return

        labels = get_label_manager()
        reaped = 0
        for issue in in_progress:
            execution = await self._db.get_execution_for_issue(str(issue.number))
            if execution and execution.status in (ExecutionStatus.PENDING, ExecutionStatus.RUNNING):
                continue  # Active execution — skip

            logger.warning(f"Issue #{issue.number}: ag/in-progress but no active execution — reaping to ag/failed")
            try:
                await labels.transition_to(repo, str(issue.number), "ag/failed")
            except Exception as e:
                logger.warning(f"Failed to reap issue #{issue.number}: {e}")
                continue
            await self._db.record_pipeline_event(
                issue_number=issue.number,
                repo=repo,
                event_type="stale_reaped",
                stage="monitor",
                detail={"reason": "in-progress with no active execution"},
            )
            reaped += 1

        if reaped:
            logger.info(f"Phase 4b: Reaped {reaped} stale in-progress issues")

    async def _auto_retry_failed(self, repo: str) -> None:
        """Phase 4c: Auto-retry ag/failed issues that haven't exhausted retries.

        Finds open issues labelled ag/failed, checks retry_count against
        max_retries_per_issue, and re-launches them as fresh implement attempts.
        """
        from ..issue_tracker.public_api import IssueStatus

        tracker = get_issue_tracker()
        all_open = await tracker.list_issues(repo, status=IssueStatus.OPEN)
        failed = [i for i in all_open if "ag/failed" in i.labels]

        if not failed:
            return

        budget = get_budget_manager()
        labels = get_label_manager()
        launcher = get_agent_launcher()
        retried = 0

        for issue in failed:
            if retried >= settings.max_auto_retries_per_cycle:
                logger.info(f"Auto-retry: per-cycle cap ({settings.max_auto_retries_per_cycle}) reached, stopping")
                break

            can_launch, reason = await budget.can_launch_agent()
            if not can_launch:
                logger.info(f"Auto-retry: budget limit reached ({reason}), stopping")
                break

            issue_state = await self._db.get_issue_state(issue.number, repo)
            retry_count = (issue_state or {}).get("retry_count", 0)
            if retry_count >= settings.max_retries_per_issue:
                continue

            if await launcher.has_active_execution(str(issue.number)):
                continue

            # Fetch checkpoint from previous attempt if available
            checkpoint = await self._db.get_latest_checkpoint(str(issue.number))
            context = {}
            if checkpoint:
                context["what_not_to_do"] = checkpoint.get("context_summary", "")

            reviewer = await launcher.resolve_reviewer(repo, issue)
            if reviewer:
                context["reviewer"] = reviewer

            from .prompt_builder import build_prompt

            prompt = build_prompt(issue, repo, mode="implement", context=context, checkpoint=checkpoint)
            await labels.transition_to(repo, str(issue.number), "ag/in-progress")

            launched = await launcher.claim_and_launch(
                issue_id=str(issue.number),
                repo_url=f"https://github.com/{repo}.git",
                prompt=prompt,
                mode="implement",
                issue_number=issue.number,
            )
            if launched:
                await self._db.upsert_issue_state(
                    issue_number=issue.number,
                    repo=repo,
                    retry_count=retry_count + 1,
                )
                await self._db.record_pipeline_event(
                    issue_number=issue.number,
                    repo=repo,
                    event_type="auto_retry",
                    stage="retry",
                    detail={"retry_count": retry_count + 1, "max_retries": settings.max_retries_per_issue},
                )
                logger.info(f"Issue #{issue.number}: auto-retry #{retry_count + 1} — launched agent")
                retried += 1
            else:
                # Revert label if launch failed
                try:
                    await labels.transition_to(repo, str(issue.number), "ag/failed")
                except Exception:
                    pass

        if retried:
            logger.info(f"Phase 4c: Auto-retried {retried} failed issues")

    async def _check_merge_conflicts(self, repo: str, launcher) -> None:
        """Phase 5b: Check open agent PRs for merge conflicts.

        For each open PR on an agent/* branch, fetches the individual PR to
        check the `mergeable` field. If the PR has conflicts, launches a
        rebase agent to resolve them.
        """
        prs = await self._tracker.list_open_prs(repo, per_page=100)
        rebased = 0

        for pr in prs:
            head_branch = pr.get("head", {}).get("ref", "")
            if not head_branch.startswith("agent/"):
                continue

            pr_number = pr["number"]

            match = re.match(r"agent/(\d+)(?:-|$)", head_branch)
            if not match:
                continue
            issue_id = match.group(1)

            # Fetch individual PR to get mergeable status
            pr_data = await self._tracker.get_pr_data(repo, pr_number)
            if not pr_data:
                continue

            mergeable = pr_data.get("mergeable")
            if mergeable is None or mergeable:
                continue  # No conflicts or not computed yet

            # Dedup: don't rebase the same HEAD SHA twice
            issue_state = await self._db.get_issue_state(int(issue_id), repo)
            metadata = (issue_state or {}).get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            head_sha = pr_data.get("head", {}).get("sha", "")
            if head_sha and metadata.get("last_rebase_sha") == head_sha:
                continue

            launched = await launcher.launch_rebase(
                repo,
                {
                    "pr_number": pr_number,
                    "issue_id": issue_id,
                    "branch": head_branch,
                },
            )
            if launched:
                await self._db.merge_issue_metadata(
                    issue_number=int(issue_id),
                    repo=repo,
                    metadata_update={"last_rebase_sha": head_sha},
                )
                rebased += 1

        if rebased:
            logger.info(f"Phase 5b: Launched {rebased} rebase agents for merge conflicts")

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
