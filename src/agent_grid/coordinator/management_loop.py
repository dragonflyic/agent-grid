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
        """Run one full cycle of all 9 phases."""
        repo = settings.target_repo
        if not repo:
            logger.warning("No target_repo configured, skipping cycle")
            return

        logger.info(f"=== Starting cron cycle for {repo} ===")

        # Phase 1: Scan
        scanner = get_scanner()
        candidates = await scanner.scan(repo)
        logger.info(f"Phase 1: Found {len(candidates)} candidate issues")

        # Phase 2 + 3: Classify, quality-gate, and act
        classifier = get_classifier()
        budget = get_budget_manager()
        labels = get_label_manager()

        for issue in candidates:
            can_launch, reason = await budget.can_launch_agent()
            if not can_launch:
                logger.info(f"Budget limit reached: {reason}. Stopping new assignments.")
                await self._db.record_pipeline_event(issue.number, repo, "budget_blocked", "launch", {"reason": reason})
                break

            classification = await classifier.classify(issue)

            # Save classification to DB
            await self._db.upsert_issue_state(
                issue_number=issue.number,
                repo=repo,
                classification=classification.category,
            )
            await self._db.record_pipeline_event(
                issue.number,
                repo,
                "classified",
                "classify",
                {
                    "category": classification.category,
                    "reason": classification.reason,
                    "estimated_complexity": classification.estimated_complexity,
                },
            )

            if classification.category == "SKIP":
                await labels.transition_to(repo, issue.id, "ag/skipped")
                await self._tracker.add_comment(
                    repo,
                    issue.id,
                    f"Skipping automated work: {classification.reason}",
                )
                logger.info(f"Issue #{issue.number}: SKIPPED — {classification.reason}")
                continue

            if classification.category == "BLOCKED":
                await labels.transition_to(repo, issue.id, "ag/blocked")
                question = classification.blocking_question or classification.reason
                comment = embed_metadata(
                    f"**Agent needs clarification:**\n\n{question}",
                    {"type": "blocked", "reason": classification.reason},
                )
                await self._tracker.add_comment(repo, issue.id, comment)
                logger.info(f"Issue #{issue.number}: BLOCKED — posted question")
                continue

            # Quality gate for SIMPLE and COMPLEX issues
            if settings.quality_gate_enabled:
                gate_result = await self._run_quality_gate(repo, issue, classification, is_proactive=False)
                if gate_result == "blocked":
                    continue
                if gate_result == "skipped":
                    continue

            if classification.category == "SIMPLE":
                await self._launch_simple(repo, issue)
            elif classification.category == "COMPLEX":
                await self._launch_planner(repo, issue)

        # Phase 4: Monitor in-progress
        await self._check_in_progress(repo)
        await self._reap_stale_in_progress(repo)

        # Phase 4c: Auto-retry failed issues that haven't exhausted retries
        await self._auto_retry_failed(repo)

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

            # Enforce retry limit (same as webhook path)
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

            check_info = await self._enrich_check_output(repo, check_info)
            launched = await self._launch_ci_fix(repo, check_info)
            if launched:
                # Update dedup cursor + fix count (mirrors webhook path)
                updated_meta = {
                    **ci_meta,
                    "last_ci_check_sha": check_info.get("head_sha", ""),
                    "ci_fix_count": ci_fix_count + 1,
                }
                await self._db.upsert_issue_state(
                    issue_number=int(ci_issue_id),
                    repo=repo,
                    metadata=updated_meta,
                )
                ci_launched += 1
        if ci_launched:
            logger.info(f"Phase 7: Launched {ci_launched} CI fix agents")

        # Phase 8: Resolve blockers — launch agents directly for unblocked issues
        blocker_resolver = get_blocker_resolver()
        unblocked = await blocker_resolver.check_blocked_issues(repo)
        for issue in unblocked:
            await self._launch_unblocked(repo, issue)
        if unblocked:
            logger.info(f"Phase 8: Launched {len(unblocked)} unblocked issues")

        # Bonus: Check dependency resolution
        dep_resolver = get_dependency_resolver()
        await dep_resolver.check_dependencies(repo)
        await dep_resolver.check_parent_completion(repo)

        # Phase 9: Proactive scan (runs every N cycles)
        if settings.proactive_scan_enabled:
            await self._maybe_run_proactive_scan(repo)

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
            mode=mode,
            started_at=utc_now(),
        )

        claimed = await self._db.try_claim_issue(execution, issue_id=issue_id)
        if not claimed:
            logger.info(f"Issue #{issue_id}: already has active execution, skipping")
            repo = repo_url.replace("https://github.com/", "").replace(".git", "")
            await self._db.record_pipeline_event(
                int(issue_id) if issue_id.isdigit() else 0,
                repo,
                "launch_failed",
                "launch",
                {"reason": "duplicate_execution"},
            )
            return False

        config = ExecutionConfig(repo_url=repo_url, prompt=prompt)
        grid = get_execution_grid()
        try:
            from ..execution_grid.fly_grid import FlyExecutionGrid
            from ..execution_grid.oz_grid import OzExecutionGrid

            if isinstance(grid, (FlyExecutionGrid, OzExecutionGrid)):
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
            repo = repo_url.replace("https://github.com/", "").replace(".git", "")
            await self._db.record_pipeline_event(
                int(issue_id) if issue_id.isdigit() else 0,
                repo,
                "launch_failed",
                "launch",
                {"reason": str(e)},
            )
            return False

        # Record successful launch
        repo = repo_url.replace("https://github.com/", "").replace(".git", "")
        await self._db.record_pipeline_event(
            int(issue_id) if issue_id.isdigit() else 0,
            repo,
            "launched",
            "launch",
            {"mode": mode, "execution_id": str(execution_id)},
        )
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

    async def _enrich_check_output(self, repo: str, check_info: dict) -> dict:
        """If check_output is empty, fetch actual CI logs via job ID."""
        if check_info.get("check_output") or not check_info.get("job_id"):
            return check_info
        try:
            from ..issue_tracker.github_client import GitHubClient

            tracker = get_issue_tracker()
            if isinstance(tracker, GitHubClient):
                logs = await tracker.get_actions_job_logs(repo, check_info["job_id"])
                if logs:
                    return {**check_info, "check_output": logs}
        except Exception as e:
            logger.warning(f"Failed to fetch CI logs for job {check_info.get('job_id')}: {e}")
        return check_info

    async def _launch_ci_fix(self, repo: str, check_info: dict) -> bool:
        """Launch an agent to fix a failing CI check on an agent PR.

        Returns True if agent was launched, False otherwise.
        """
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

    async def _run_quality_gate(
        self,
        repo: str,
        issue,
        classification,
        is_proactive: bool,
    ) -> str:
        """Run the quality gate on an issue.

        Returns "proceed", "blocked", or "skipped".
        """
        from .quality_gate import get_quality_gate

        quality_gate = get_quality_gate()
        labels = get_label_manager()

        assessment = await quality_gate.evaluate(
            issue=issue,
            classification=classification,
            is_proactive=is_proactive,
        )

        # Store assessment in issue_state metadata
        # Read existing metadata first to avoid overwriting other fields
        issue_state = await self._db.get_issue_state(issue.number, repo)
        existing_metadata = (issue_state or {}).get("metadata") or {}
        if isinstance(existing_metadata, str):
            import json

            existing_metadata = json.loads(existing_metadata)

        updated_metadata = {
            **existing_metadata,
            "confidence_score": assessment.score,
            "confidence_verdict": assessment.verdict,
            "risk_flags": assessment.risk_flags,
            "green_flags": assessment.green_flags,
        }

        await self._db.upsert_issue_state(
            issue_number=issue.number,
            repo=repo,
            metadata=updated_metadata,
        )

        if quality_gate.should_clarify(assessment, is_proactive):
            await labels.transition_to(repo, issue.id, "ag/blocked")
            question = assessment.clarification_question or assessment.explanation
            owner_tag = f"@{issue.author} " if issue.author else ""
            comment = embed_metadata(
                f"**Agent confidence check — needs clarification:**\n\n"
                f"{owner_tag}{question}\n\n"
                f"_Confidence: {assessment.score}/10. "
                f"Risk flags: {', '.join(assessment.risk_flags)}_",
                {"type": "blocked", "reason": f"quality_gate: {assessment.explanation}"},
            )
            await self._tracker.add_comment(repo, issue.id, comment)
            logger.info(
                f"Issue #{issue.number}: quality gate blocked "
                f"(score={assessment.score}/10, flags={assessment.risk_flags})"
            )
            await self._db.record_pipeline_event(
                issue.number,
                repo,
                "quality_gate",
                "quality_gate",
                {"score": assessment.score, "verdict": "blocked", "risk_flags": assessment.risk_flags},
            )
            return "blocked"

        if not quality_gate.should_proceed(assessment, is_proactive):
            if not is_proactive:
                await labels.transition_to(repo, issue.id, "ag/skipped")
                await self._tracker.add_comment(
                    repo,
                    issue.id,
                    f"Skipping: confidence too low ({assessment.score}/10). {assessment.explanation}",
                )
            logger.info(f"Issue #{issue.number}: quality gate skipped (score={assessment.score}/10)")
            await self._db.record_pipeline_event(
                issue.number,
                repo,
                "quality_gate",
                "quality_gate",
                {"score": assessment.score, "verdict": "skipped", "is_proactive": is_proactive},
            )
            return "skipped"

        await self._db.record_pipeline_event(
            issue.number,
            repo,
            "quality_gate",
            "quality_gate",
            {
                "score": assessment.score,
                "verdict": "proceed",
                "risk_flags": assessment.risk_flags,
                "green_flags": assessment.green_flags,
            },
        )
        return "proceed"

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

        candidates = await proactive_scanner.scan(repo)
        picked_up = 0

        for issue in candidates:
            if picked_up >= settings.proactive_max_per_cycle:
                break

            can_launch, reason = await budget.can_launch_agent()
            if not can_launch:
                logger.info(f"Proactive scan: budget limit reached: {reason}")
                break

            # Classify first
            classification = await classifier.classify(issue)

            await self._db.upsert_issue_state(
                issue_number=issue.number,
                repo=repo,
                classification=classification.category,
            )

            if classification.category in ("BLOCKED", "SKIP"):
                await self._db.upsert_issue_state(
                    issue_number=issue.number,
                    repo=repo,
                    metadata={"proactive_skipped": True},
                )
                continue

            # Run quality gate with proactive=True (requires high score)
            gate_result = await self._run_quality_gate(repo, issue, classification, is_proactive=True)

            if gate_result != "proceed":
                # Mark as skipped so we don't re-evaluate next cycle
                issue_state = await self._db.get_issue_state(issue.number, repo)
                existing_metadata = (issue_state or {}).get("metadata") or {}
                if isinstance(existing_metadata, str):
                    import json

                    existing_metadata = json.loads(existing_metadata)
                await self._db.upsert_issue_state(
                    issue_number=issue.number,
                    repo=repo,
                    metadata={**existing_metadata, "proactive_skipped": True},
                )
                continue

            # Passed the gate — pick it up
            logger.info(f"Proactive pickup: Issue #{issue.number}")

            # Add ag/proactive as an informational label (persists through transitions)
            await labels.add_label(repo, issue.id, "ag/proactive")

            # Comment tagging the owner
            owner_tag = f"@{issue.author}" if issue.author else "the issue author"
            await self._tracker.add_comment(
                repo,
                issue.id,
                f"I noticed this issue and I'm confident I can handle it. "
                f"Starting work now — {owner_tag}, I'll tag you for review "
                f"when the PR is ready.",
            )

            # Mark as proactively picked
            issue_state = await self._db.get_issue_state(issue.number, repo)
            existing_metadata = (issue_state or {}).get("metadata") or {}
            if isinstance(existing_metadata, str):
                import json

                existing_metadata = json.loads(existing_metadata)
            await self._db.upsert_issue_state(
                issue_number=issue.number,
                repo=repo,
                metadata={**existing_metadata, "proactive_picked": True},
            )

            # Launch agent based on classification
            if classification.category == "SIMPLE":
                await self._launch_simple(repo, issue)
            elif classification.category == "COMPLEX":
                await self._launch_planner(repo, issue)

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
        retried = 0

        for issue in failed:
            can_launch, reason = await budget.can_launch_agent()
            if not can_launch:
                logger.info(f"Auto-retry: budget limit reached ({reason}), stopping")
                break

            issue_state = await self._db.get_issue_state(issue.number, repo)
            retry_count = (issue_state or {}).get("retry_count", 0)
            if retry_count >= settings.max_retries_per_issue:
                continue

            if await self._has_active_execution(str(issue.number)):
                continue

            # Fetch checkpoint from previous attempt if available
            checkpoint = await self._db.get_latest_checkpoint(str(issue.number))
            context = {}
            if checkpoint:
                context["what_not_to_do"] = checkpoint.get("context_summary", "")

            prompt = build_prompt(issue, repo, mode="implement", context=context, checkpoint=checkpoint)
            await labels.transition_to(repo, str(issue.number), "ag/in-progress")

            launched = await self._claim_and_launch(
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
