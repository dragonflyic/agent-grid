"""Scheduler for event-driven agent reactions.

The management loop handles the cron-based scanning/classification/launching
pipeline. The scheduler focuses on real-time event-driven reactions:
- Webhook-triggered issue creation → classify + launch immediately
- Issue comments on ag/* issues → unblock or trigger re-work
- PR review comments → launch address_review agent
- PR comments (regular) → launch address_review agent on agent branches
- PR closed → launch retry agent
- Nudge requests → immediate agent launch
- Agent completion → save checkpoint, update labels
- Agent failure → update labels
"""

import logging
import re
from uuid import UUID

from ..execution_grid import (
    Event,
    EventType,
    ExecutionStatus,
    event_bus,
    utc_now,
)
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.label_manager import get_label_manager
from .budget_manager import get_budget_manager
from .database import get_database
from .nudge_handler import get_nudge_handler
from .prompt_builder import build_prompt

logger = logging.getLogger("agent_grid.scheduler")


class Scheduler:
    """
    Decides when to launch agents based on real-time events.

    Listens for:
    - New issues created or labeled (webhook)
    - Issue comments on ag/* issues
    - PR review submissions on agent branches
    - PR closed events on agent branches
    - Nudge requests
    - Execution completions/failures
    """

    def __init__(self):
        self._db = get_database()
        self._budget_manager = get_budget_manager()
        self._nudge_handler = get_nudge_handler()
        self._running = False

    async def start(self) -> None:
        """Start the scheduler and subscribe to events."""
        self._running = True
        event_bus.subscribe(self._handle_event)

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        event_bus.unsubscribe(self._handle_event)

    async def _handle_event(self, event: Event) -> None:
        """Handle incoming events."""
        if not self._running:
            return

        try:
            if event.type == EventType.ISSUE_CREATED:
                await self._handle_issue_created(event)
            elif event.type == EventType.ISSUE_UPDATED:
                await self._handle_issue_updated(event)
            elif event.type == EventType.ISSUE_COMMENT:
                await self._handle_issue_comment(event)
            elif event.type == EventType.NUDGE_REQUESTED:
                await self._handle_nudge_requested(event)
            elif event.type == EventType.AGENT_STARTED:
                await self._handle_agent_started(event)
            elif event.type == EventType.AGENT_COMPLETED:
                await self._handle_agent_completed(event)
            elif event.type == EventType.AGENT_FAILED:
                await self._handle_agent_failed(event)
            elif event.type == EventType.PR_REVIEW:
                await self._handle_pr_review(event)
            elif event.type == EventType.PR_COMMENT:
                await self._handle_pr_comment(event)
            elif event.type == EventType.PR_CLOSED:
                await self._handle_pr_closed(event)
            elif event.type == EventType.CHECK_RUN_FAILED:
                await self._handle_check_run_failed(event)
        except Exception:
            logger.exception(f"Error handling event {event.type}")

    # -------------------------------------------------------------------------
    # Issue events
    # -------------------------------------------------------------------------

    async def _handle_issue_created(self, event: Event) -> None:
        """Handle new issue creation — classify and act immediately.

        When an issue is created with an ag/* label, classify it and launch
        the appropriate agent without waiting for the cron loop.
        """
        payload = event.payload
        issue_id = payload.get("issue_id")
        repo = payload.get("repo")
        labels = payload.get("labels", [])

        if not repo or not issue_id:
            return

        # Issue must be opted in with an ag/* label
        if not self._should_auto_launch(labels):
            return

        # If it's already in a handled state, skip
        from .scanner import HANDLED_LABELS

        if any(label in HANDLED_LABELS for label in labels):
            return

        await self._classify_and_act(repo, issue_id)

    async def _handle_issue_updated(self, event: Event) -> None:
        """Handle issue updated (labeled/unlabeled/edited).

        Reacts when ag/todo label is added to an existing issue.
        """
        payload = event.payload
        action = payload.get("action")
        repo = payload.get("repo")
        issue_id = payload.get("issue_id")
        labels = payload.get("labels", [])

        if action != "labeled" or not repo or not issue_id:
            return

        # Only react if ag/todo is present (just got added)
        if "ag/todo" not in labels:
            return

        # Don't process if also in a handled state
        from .scanner import HANDLED_LABELS

        if any(label in HANDLED_LABELS for label in labels):
            return

        await self._classify_and_act(repo, issue_id)

    async def _handle_issue_comment(self, event: Event) -> None:
        """Handle human comment on an ag/* issue.

        If the issue is ag/blocked, check if this is the human reply that
        unblocks it, then launch agent with clarification context.
        """
        payload = event.payload
        repo = payload.get("repo")
        issue_id = payload.get("issue_id")
        labels = payload.get("labels", [])
        is_pull_request = payload.get("is_pull_request", False)

        if not repo or not issue_id:
            return

        # PR comments are handled by the PR_REVIEW flow
        if is_pull_request:
            return

        # If the issue is blocked, this comment might unblock it
        if "ag/blocked" in labels:
            await self._handle_blocked_issue_comment(repo, issue_id)

    async def _handle_blocked_issue_comment(self, repo: str, issue_id: str) -> None:
        """Handle a comment on a blocked issue — potentially unblocks it."""
        from .blocker_resolver import get_blocker_resolver

        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
        except Exception as e:
            logger.error(f"Failed to fetch blocked issue {issue_id}: {e}")
            return

        resolver = get_blocker_resolver()
        if resolver._has_human_reply_after_block(issue.comments):
            logger.info(f"Issue #{issue_id} unblocked via webhook — launching agent")
            from .agent_launcher import get_agent_launcher

            launcher = get_agent_launcher()
            await launcher.launch_unblocked(repo, issue)

    # -------------------------------------------------------------------------
    # PR events
    # -------------------------------------------------------------------------

    async def _handle_pr_review(self, event: Event) -> None:
        """Handle PR review submission — launch address_review agent."""
        payload = event.payload
        repo = payload.get("repo")
        pr_number = payload.get("pr_number")
        branch = payload.get("branch", "")
        review_state = payload.get("review_state")

        if not repo or not pr_number:
            return

        # Only react to actionable reviews
        if review_state not in ("changes_requested", "commented"):
            return

        # Extract issue ID from agent branch name (agent/42 → "42")
        match = re.match(r"agent/(\d+)(?:-|$)", branch)
        if not match:
            return

        # Use PR monitor to get the full review comments
        from .pr_monitor import get_pr_monitor

        pr_monitor = get_pr_monitor()
        prs_needing_work = await pr_monitor.check_prs(repo, update_timestamp=False)

        for pr_info in prs_needing_work:
            if pr_info["pr_number"] == pr_number and pr_info["issue_id"]:
                from .agent_launcher import get_agent_launcher

                launcher = get_agent_launcher()
                await launcher.launch_review_handler(repo, pr_info)
                break

    async def _handle_pr_comment(self, event: Event) -> None:
        """Handle regular PR comment — launch address_review if on an agent branch."""
        payload = event.payload
        repo = payload.get("repo")
        pr_number = payload.get("pr_number")
        comment_body = payload.get("comment_body", "")

        if not repo or not pr_number:
            return

        # Fetch the PR to get the branch name
        tracker = get_issue_tracker()
        pr_data = await tracker.get_pr_data(repo, pr_number)
        if not pr_data:
            return

        head_branch = pr_data.get("head", {}).get("ref", "")
        if not head_branch.startswith("agent/"):
            return

        match = re.match(r"agent/(\d+)(?:-|$)", head_branch)
        if not match:
            return
        issue_id = match.group(1)

        # Build pr_info and launch via the review handler
        pr_info = {
            "pr_number": pr_number,
            "issue_id": issue_id,
            "branch": head_branch,
            "review_comments": comment_body,
        }

        from .agent_launcher import get_agent_launcher

        launcher = get_agent_launcher()
        await launcher.launch_review_handler(repo, pr_info)
        logger.info(f"PR #{pr_number}: launched agent from PR comment")

    async def _handle_pr_closed(self, event: Event) -> None:
        """Handle PR closed — transition to ag/done if merged, retry if not."""
        payload = event.payload
        repo = payload.get("repo")
        pr_number = payload.get("pr_number")
        branch = payload.get("branch", "")
        merged = payload.get("merged", False)

        if not repo or not pr_number:
            return

        match = re.match(r"agent/(\d+)(?:-|$)", branch)
        if not match:
            return
        issue_id = match.group(1)

        if merged:
            # Success — transition issue to ag/done and close it
            from ..issue_tracker import IssueStatus

            labels_mgr = get_label_manager()
            await labels_mgr.transition_to(repo, issue_id, "ag/done")
            # Mirror label onto PR
            tracker = get_issue_tracker()
            await tracker.add_label(repo, str(pr_number), "ag/done")
            await tracker.update_issue_status(repo, issue_id, IssueStatus.CLOSED)
            await self._update_status(repo, issue_id, "pr_merged", f"PR #{pr_number} has been merged.")
            logger.info(f"PR #{pr_number} merged — issue #{issue_id} marked ag/done")

            # Check if this is a sub-issue — advance the queue
            await self._advance_sub_issue_queue(repo, issue_id)
            return

        # Not merged — launch retry agent
        from .pr_monitor import get_pr_monitor

        pr_monitor = get_pr_monitor()
        closed_prs = await pr_monitor.check_closed_prs(repo)

        for pr_info in closed_prs:
            if pr_info["pr_number"] == pr_number and pr_info["issue_id"]:
                from .agent_launcher import get_agent_launcher

                launcher = get_agent_launcher()
                await launcher.launch_retry(repo, pr_info)
                break

    async def _handle_check_run_failed(self, event: Event) -> None:
        """Handle CI check failure on an agent PR — launch fix agent."""
        from ..config import settings

        payload = event.payload
        repo = payload.get("repo")
        branch = payload.get("branch", "")
        head_sha = payload.get("head_sha", "")

        if not repo or not branch:
            return

        # Extract issue ID from agent branch name
        match = re.match(r"agent/(\d+)(?:-|$)", branch)
        if not match:
            return
        issue_id = match.group(1)

        # Deduplicate: skip if we already processed this SHA
        issue_state = await self._db.get_issue_state(int(issue_id), repo)
        metadata = (issue_state or {}).get("metadata") or {}
        if isinstance(metadata, str):
            import json

            metadata = json.loads(metadata)

        if head_sha and metadata.get("last_ci_check_sha") == head_sha:
            logger.info(f"CI fix already attempted for SHA {head_sha[:8]}, skipping")
            return

        # Check CI fix retry limit (max fix cycles per issue, resets on new non-agent commit)
        ci_fix_count = metadata.get("ci_fix_count", 0)
        if ci_fix_count >= settings.max_ci_fix_retries:
            logger.warning(f"Issue #{issue_id}: CI fix retry limit ({settings.max_ci_fix_retries}) reached")
            tracker = get_issue_tracker()
            check_name = payload.get("check_name")
            await tracker.add_comment(
                repo,
                issue_id,
                f"CI check `{check_name}` keeps failing after "
                f"{ci_fix_count} auto-fix attempts. Needs human intervention.",
            )
            labels_mgr = get_label_manager()
            await labels_mgr.transition_to(repo, issue_id, "ag/failed")
            return

        # Fetch actual CI logs if check_output is empty (GitHub Actions doesn't populate output fields)
        from .agent_launcher import get_agent_launcher

        launcher = get_agent_launcher()
        payload = await launcher.enrich_check_output(repo, payload)

        # Launch CI fix agent
        launched = await launcher.launch_ci_fix(repo, payload)

        if not launched:
            logger.info(f"Issue #{issue_id}: CI fix agent not launched (active execution or claim failed)")
            return

        # Only update metadata after successful launch (atomic merge)
        await self._db.merge_issue_metadata(
            issue_number=int(issue_id),
            repo=repo,
            metadata_update={"last_ci_check_sha": head_sha, "ci_fix_count": ci_fix_count + 1},
        )
        logger.info(f"Issue #{issue_id}: CI fix #{ci_fix_count + 1} launched for '{payload.get('check_name')}'")

    # -------------------------------------------------------------------------
    # Classification + launch (shared by issue_created and issue_updated)
    # -------------------------------------------------------------------------

    async def _classify_and_act(self, repo: str, issue_id: str) -> None:
        """Classify an issue, run quality gate, and launch the appropriate agent."""
        from ..config import settings

        can_launch, reason = await self._budget_manager.can_launch_agent()
        if not can_launch:
            logger.warning(f"Budget check failed for webhook issue: {reason}")
            return

        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
        except Exception as e:
            logger.error(f"Failed to fetch issue {issue_id}: {e}")
            return

        from ..issue_tracker.metadata import embed_metadata
        from .classifier import get_classifier

        classifier = get_classifier()
        classification = await classifier.classify(issue, repo)

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
                "source": "webhook",
            },
        )

        from .agent_launcher import get_agent_launcher

        launcher = get_agent_launcher()
        labels = get_label_manager()

        if classification.category == "SKIP":
            await labels.transition_to(repo, issue.id, "ag/skipped")
            await tracker.add_comment(
                repo,
                issue.id,
                f"Skipping automated work: {classification.reason}",
            )
            logger.info(f"Webhook: Issue #{issue.number}: SKIPPED")
            return

        if classification.category == "BLOCKED":
            await labels.transition_to(repo, issue.id, "ag/blocked")
            question = classification.blocking_question or classification.reason
            comment = embed_metadata(
                f"**Agent needs clarification:**\n\n{question}",
                {"type": "blocked", "reason": classification.reason},
            )
            await tracker.add_comment(repo, issue.id, comment)
            logger.info(f"Webhook: Issue #{issue.number}: BLOCKED — posted question")
            return

        # Quality gate for SIMPLE and COMPLEX issues
        if settings.quality_gate_enabled:
            gate_result = await launcher.run_quality_gate(repo, issue, classification, is_proactive=False)
            if gate_result != "proceed":
                logger.info(f"Webhook: Issue #{issue.number}: quality gate {gate_result}")
                return

        if classification.category == "SIMPLE":
            await launcher.launch_simple(repo, issue)
        elif classification.category == "COMPLEX":
            await launcher.launch_planner(repo, issue)

        logger.info(f"Webhook: Processed issue #{issue.number} as {classification.category}")

    # -------------------------------------------------------------------------
    # Nudge handling
    # -------------------------------------------------------------------------

    async def _handle_nudge_requested(self, event: Event) -> None:
        """Handle nudge request."""
        payload = event.payload
        issue_id = payload.get("issue_id")
        repo = payload.get("repo")
        logger.info(f"Handling nudge request: issue_id={issue_id}, repo={repo}")

        if not issue_id:
            logger.warning("Nudge request missing issue_id")
            return

        if not repo:
            nudge_id = payload.get("nudge_id")
            if nudge_id:
                nudges = await self._nudge_handler.get_pending_nudges(limit=100)
                for nudge in nudges:
                    if str(nudge.id) == nudge_id and nudge.source_execution_id:
                        source_exec = await self._db.get_execution(nudge.source_execution_id)
                        if source_exec:
                            repo = self._extract_repo_from_url(source_exec.repo_url)
                            break

        if repo:
            await self._try_launch_agent(issue_id=issue_id, repo=repo)

    # -------------------------------------------------------------------------
    # Agent completion/failure
    # -------------------------------------------------------------------------

    async def _handle_agent_started(self, event: Event) -> None:
        """Handle agent start — transition DB status from PENDING to RUNNING."""
        payload = event.payload
        execution_id = payload.get("execution_id")
        if not execution_id:
            return

        exec_uuid = UUID(execution_id)
        execution = await self._db.get_execution(exec_uuid)
        if execution and execution.status == ExecutionStatus.PENDING:
            execution.status = ExecutionStatus.RUNNING
            execution.started_at = execution.started_at or utc_now()
            await self._db.update_execution(execution)

    async def _handle_agent_completed(self, event: Event) -> None:
        """Handle agent completion — save checkpoint and update labels."""
        payload = event.payload
        execution_id = payload.get("execution_id")

        if execution_id:
            exec_uuid = UUID(execution_id)
            checkpoint = payload.get("checkpoint")
            pr_number = payload.get("pr_number")
            branch = payload.get("branch")

            # Use update_execution_result when we have structured data (PR, branch, checkpoint)
            if pr_number or branch or checkpoint:
                await self._db.update_execution_result(
                    execution_id=exec_uuid,
                    status=ExecutionStatus.COMPLETED,
                    result=payload.get("result"),
                    pr_number=pr_number,
                    branch=branch,
                    checkpoint=checkpoint,
                )
            else:
                execution = await self._db.get_execution(exec_uuid)
                if execution:
                    execution.status = ExecutionStatus.COMPLETED
                    execution.result = payload.get("result")
                    await self._db.update_execution(execution)

            issue_id = await self._db.get_issue_id_for_execution(exec_uuid)

            # Save checkpoint if present
            if checkpoint and issue_id:
                await self._db.save_checkpoint(exec_uuid, checkpoint)

            # Update labels based on execution mode
            if issue_id:
                execution = await self._db.get_execution(exec_uuid)
                if execution:
                    repo = self._extract_repo_from_url(execution.repo_url)
                    if repo:
                        labels_mgr = get_label_manager()

                        if execution.mode == "plan":
                            # Planning done — transition to epic, sub-issues auto-launch
                            await labels_mgr.transition_to(repo, issue_id, "ag/epic")
                            logger.info(f"Plan completed for issue #{issue_id} — transitioned to ag/epic")
                        elif execution.mode == "scout":
                            # Scout done — parse result and act on verdict
                            from .agent_launcher import get_agent_launcher
                            launcher = get_agent_launcher()
                            scout_result = launcher.parse_scout_result(execution.result or "")
                            if scout_result:
                                await launcher.handle_scout_completed(
                                    repo, issue_id, execution.id, scout_result
                                )
                            else:
                                # Scout didn't produce parseable output — fall back to implement
                                logger.warning(f"Issue #{issue_id}: scout result not parseable, falling back to implement")
                                await launcher.handle_scout_completed(
                                    repo, issue_id, execution.id,
                                    {"verdict": "implement", "plan": execution.result or "", "reason": "scout output fallback"},
                                )
                        elif execution.mode == "rebase":
                            # Rebase done — just mark for review like implementation
                            await labels_mgr.transition_to(repo, issue_id, "ag/review-pending")
                            if pr_number:
                                tracker = get_issue_tracker()
                                await tracker.add_label(repo, str(pr_number), "ag/review-pending")
                            detail = f"PR #{pr_number} rebased." if pr_number else None
                            stage = "pr_created" if pr_number else "review_pending"
                            await self._update_status(repo, issue_id, stage, detail)
                        else:
                            # Implementation done — mark for review and notify owner
                            await labels_mgr.transition_to(repo, issue_id, "ag/review-pending")
                            # Mirror label onto the PR itself for filtering
                            if pr_number:
                                tracker = get_issue_tracker()
                                await tracker.add_label(repo, str(pr_number), "ag/review-pending")
                            await self._assign_and_tag_owner(repo, issue_id, pr_number)

                        # Update status comment (skip for scout/rebase — they handle their own)
                        if execution.mode not in ("scout", "rebase"):
                            if pr_number:
                                detail = f"PR #{pr_number} created."
                                stage = "pr_created"
                            elif branch:
                                detail = (
                                    f"Implementation pushed to branch `{branch}` "
                                    f"but no PR was created automatically. "
                                    f"Please create a PR manually from this branch."
                                )
                                stage = "review_pending"
                            else:
                                detail = None
                                stage = "completed" if execution.mode == "plan" else "review_pending"
                            await self._update_status(repo, issue_id, stage, detail)

        # Process any pending nudges now that we have capacity
        await self._process_pending_nudges()

    async def _assign_and_tag_owner(self, repo: str, issue_id: str, pr_number: int | None = None) -> None:
        """Assign the issue to its author, request PR review, and comment on the PR."""
        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
            if not issue.author:
                return

            await tracker.assign_issue(repo, issue_id, issue.author)

            if pr_number:
                await tracker.request_pr_reviewers(repo, pr_number, [issue.author])
                await tracker.add_pr_comment(
                    repo,
                    pr_number,
                    f"@{issue.author} — this PR is ready for your review.",
                )

            pr_ref = f" PR #{pr_number}" if pr_number else ""
            await tracker.add_comment(
                repo,
                issue_id,
                f"@{issue.author} — implementation is ready for your review.{pr_ref}",
            )
            logger.info(f"Issue #{issue_id}: assigned to @{issue.author}, requested review on PR #{pr_number}")
        except Exception as e:
            logger.warning(f"Failed to assign/tag owner for issue #{issue_id}: {e}")

    async def _handle_agent_failed(self, event: Event) -> None:
        """Handle agent failure — update labels."""
        payload = event.payload
        execution_id = payload.get("execution_id")

        if execution_id:
            execution = await self._db.get_execution(UUID(execution_id))
            if execution:
                execution.status = ExecutionStatus.FAILED
                execution.result = payload.get("error")
                await self._db.update_execution(execution)

                # Update label to failed
                issue_id = await self._db.get_issue_id_for_execution(UUID(execution_id))
                if issue_id:
                    repo = self._extract_repo_from_url(execution.repo_url)
                    if repo:
                        labels_mgr = get_label_manager()
                        await labels_mgr.transition_to(repo, issue_id, "ag/failed")
                        error_msg = payload.get("error", "")
                        detail = f"Agent failed: {error_msg}" if error_msg else None
                        await self._update_status(repo, issue_id, "failed", detail)

        await self._process_pending_nudges()

    # -------------------------------------------------------------------------
    # Sub-issue queue advancement
    # -------------------------------------------------------------------------

    async def _advance_sub_issue_queue(self, repo: str, issue_id: str) -> None:
        """When a sub-issue PR is merged, activate the next queued sibling."""
        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
        except Exception:
            return

        # Check if this issue has a parent (is a sub-issue)
        if not issue.parent_id:
            return

        # Get the parent's sub-issue order from metadata
        parent_state = await self._db.get_issue_state(int(issue.parent_id), repo)
        if not parent_state:
            return
        metadata = parent_state.get("metadata") or {}
        if isinstance(metadata, str):
            import json
            metadata = json.loads(metadata)

        sub_order = metadata.get("sub_issue_order", [])
        if not sub_order:
            return

        # Find the next queued sub-issue in order
        labels_mgr = get_label_manager()
        activated = False
        for sub_num in sub_order:
            if str(sub_num) == issue_id:
                continue  # Skip the one that just merged
            try:
                sub = await tracker.get_issue(repo, str(sub_num))
                if "ag/queued" in sub.labels:
                    await labels_mgr.transition_to(repo, str(sub_num), "ag/todo")
                    logger.info(
                        f"Sub-issue #{sub_num}: activated (next in queue after #{issue_id} merged)"
                    )
                    activated = True
                    break  # Only activate one
            except Exception as e:
                logger.warning(f"Failed to check sub-issue #{sub_num}: {e}")

        # Update progress comment on parent
        if activated:
            await self._update_progress_comment(repo, issue.parent_id, sub_order)

    async def _update_progress_comment(self, repo: str, parent_id: str, sub_order: list[int]) -> None:
        """Update the progress comment on the parent issue."""
        tracker = get_issue_tracker()
        lines = [f"## Implementation Plan ({len(sub_order)} steps)\n"]

        for i, sub_num in enumerate(sub_order):
            try:
                sub = await tracker.get_issue(repo, str(sub_num))
                title = sub.title.replace(f"[Sub #{parent_id}] ", "")
                if "ag/done" in sub.labels or sub.status.value == "closed":
                    icon = "\u2705"  # check mark
                    status = "merged"
                elif "ag/in-progress" in sub.labels or "ag/review-pending" in sub.labels:
                    icon = "\U0001f7e2"  # green circle
                    status = "in progress"
                elif "ag/todo" in sub.labels:
                    icon = "\U0001f7e1"  # yellow circle
                    status = "next up"
                elif "ag/failed" in sub.labels:
                    icon = "\u274c"  # X
                    status = "failed"
                else:
                    icon = "\u23f3"  # hourglass
                    status = "queued"
                lines.append(f"{i+1}. {icon} #{sub_num} {title} — {status}")
            except Exception:
                lines.append(f"{i+1}. \u2753 #{sub_num} — unable to fetch")

        lines.append("\nSteps execute sequentially. Merge each PR to trigger the next step.")

        await self._update_status(repo, parent_id, "in_progress", "\n".join(lines))

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    async def _update_status(self, repo: str, issue_id: str, stage: str, detail: str | None = None) -> None:
        """Update the status comment on the issue (fire-and-forget)."""
        try:
            from .status_comment import get_status_comment_manager

            mgr = get_status_comment_manager()
            await mgr.post_or_update(repo, issue_id, stage, detail)
        except Exception:
            logger.warning(f"Failed to update status comment for issue #{issue_id}", exc_info=True)

    async def _try_launch_agent(self, issue_id: str, repo: str) -> bool:
        """Attempt to launch an agent for an issue.

        Uses _claim_and_launch to claim the DB row FIRST, then launch the machine.
        """
        logger.info(f"Attempting to launch agent: issue_id={issue_id}, repo={repo}")

        can_launch, reason = await self._budget_manager.can_launch_agent()
        if not can_launch:
            logger.warning(f"Budget check failed: {reason}")
            return False

        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
        except Exception as e:
            logger.error(f"Failed to get issue {issue_id} from {repo}: {e}")
            return False

        prompt = build_prompt(issue, repo, mode="implement")
        repo_url = f"https://github.com/{repo}.git"

        from .agent_launcher import get_agent_launcher

        launcher = get_agent_launcher()
        launched = await launcher.claim_and_launch(
            issue_id=issue_id,
            repo_url=repo_url,
            prompt=prompt,
            mode="implement",
            issue_number=issue.number,
        )
        if not launched:
            return False

        logger.info(f"Launched agent for issue {issue_id}")
        return True

    async def _process_pending_nudges(self) -> None:
        """Process pending nudge requests."""
        nudges = await self._nudge_handler.get_pending_nudges(limit=5)

        for nudge in nudges:
            repo = None
            if nudge.source_execution_id:
                source = await self._db.get_execution(nudge.source_execution_id)
                if source:
                    repo = self._extract_repo_from_url(source.repo_url)

            if repo:
                launched = await self._try_launch_agent(nudge.issue_id, repo)
                if launched:
                    await self._nudge_handler.mark_processed(nudge.id)

    def _should_auto_launch(self, labels: list[str]) -> bool:
        """Determine if an issue should auto-launch an agent."""
        return any(label.startswith("ag/") for label in labels)

    def _extract_repo_from_url(self, repo_url: str) -> str | None:
        """Extract owner/repo from a git URL."""
        if "github.com" in repo_url:
            parts = repo_url.replace(".git", "").split("github.com/")
            if len(parts) > 1:
                return parts[1]
        return None


# Global instance
_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
