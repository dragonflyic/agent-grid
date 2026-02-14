"""Scheduler for event-driven agent reactions.

The management loop handles the cron-based scanning/classification/launching
pipeline. The scheduler focuses on real-time event-driven reactions:
- Webhook-triggered issue creation → classify + launch immediately
- Issue comments on ag/* issues → unblock or trigger re-work
- PR review comments → launch address_review agent
- PR closed → launch retry agent
- Nudge requests → immediate agent launch
- Agent completion → save checkpoint, update labels
- Agent failure → update labels
"""

import logging
import re
from uuid import UUID

from ..execution_grid import (
    AgentExecution,
    Event,
    EventType,
    ExecutionConfig,
    ExecutionStatus,
    event_bus,
    get_execution_grid,
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
            elif event.type == EventType.AGENT_COMPLETED:
                await self._handle_agent_completed(event)
            elif event.type == EventType.AGENT_FAILED:
                await self._handle_agent_failed(event)
            elif event.type == EventType.PR_REVIEW:
                await self._handle_pr_review(event)
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
            from .management_loop import get_management_loop

            loop = get_management_loop()
            await loop._launch_unblocked(repo, issue)

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
                from .management_loop import get_management_loop

                loop = get_management_loop()
                await loop._launch_review_handler(repo, pr_info)
                break

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
            tracker = get_issue_tracker()
            await tracker.update_issue_status(repo, issue_id, IssueStatus.CLOSED)
            logger.info(f"PR #{pr_number} merged — issue #{issue_id} marked ag/done")
            return

        # Not merged — launch retry agent
        from .pr_monitor import get_pr_monitor

        pr_monitor = get_pr_monitor()
        closed_prs = await pr_monitor.check_closed_prs(repo)

        for pr_info in closed_prs:
            if pr_info["pr_number"] == pr_number and pr_info["issue_id"]:
                from .management_loop import get_management_loop

                loop = get_management_loop()
                await loop._launch_retry(repo, pr_info)
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

        # Check CI fix retry limit
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

        # Launch CI fix agent
        from .management_loop import get_management_loop

        loop = get_management_loop()
        await loop._launch_ci_fix(repo, payload)

        # Update metadata with SHA and increment count
        updated_metadata = {**metadata, "last_ci_check_sha": head_sha, "ci_fix_count": ci_fix_count + 1}
        await self._db.upsert_issue_state(
            issue_number=int(issue_id),
            repo=repo,
            metadata=updated_metadata,
        )
        logger.info(f"Issue #{issue_id}: CI fix #{ci_fix_count + 1} launched for '{payload.get('check_name')}'")

    # -------------------------------------------------------------------------
    # Classification + launch (shared by issue_created and issue_updated)
    # -------------------------------------------------------------------------

    async def _classify_and_act(self, repo: str, issue_id: str) -> None:
        """Classify an issue and launch the appropriate agent."""
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
        classification = await classifier.classify(issue)

        await self._db.upsert_issue_state(
            issue_number=issue.number,
            repo=repo,
            classification=classification.category,
        )

        from .management_loop import get_management_loop

        loop = get_management_loop()
        labels = get_label_manager()

        if classification.category == "SIMPLE":
            await loop._launch_simple(repo, issue)
        elif classification.category == "COMPLEX":
            await loop._launch_planner(repo, issue)
        elif classification.category == "BLOCKED":
            await labels.transition_to(repo, issue.id, "ag/blocked")
            question = classification.blocking_question or classification.reason
            comment = embed_metadata(
                f"**Agent needs clarification:**\n\n{question}",
                {"type": "blocked", "reason": classification.reason},
            )
            await tracker.add_comment(repo, issue.id, comment)
            logger.info(f"Webhook: Issue #{issue.number}: BLOCKED — posted question")
        elif classification.category == "SKIP":
            await labels.transition_to(repo, issue.id, "ag/skipped")
            await tracker.add_comment(
                repo,
                issue.id,
                f"Skipping automated work: {classification.reason}",
            )

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

    async def _handle_agent_completed(self, event: Event) -> None:
        """Handle agent completion — save checkpoint and update labels."""
        payload = event.payload
        execution_id = payload.get("execution_id")

        if execution_id:
            execution = await self._db.get_execution(UUID(execution_id))
            if execution:
                execution.status = ExecutionStatus.COMPLETED
                execution.result = payload.get("result")
                await self._db.update_execution(execution)

                # Save checkpoint if present
                checkpoint = payload.get("checkpoint")
                issue_id = await self._db.get_issue_id_for_execution(UUID(execution_id))
                if checkpoint and issue_id:
                    await self._db.save_checkpoint(UUID(execution_id), checkpoint)

                # Update label to review-pending
                if issue_id:
                    repo = self._extract_repo_from_url(execution.repo_url)
                    if repo:
                        labels_mgr = get_label_manager()
                        await labels_mgr.transition_to(repo, issue_id, "ag/review-pending")

        # Process any pending nudges now that we have capacity
        await self._process_pending_nudges()

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

        await self._process_pending_nudges()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    async def _try_launch_agent(self, issue_id: str, repo: str) -> UUID | None:
        """Attempt to launch an agent for an issue."""
        logger.info(f"Attempting to launch agent: issue_id={issue_id}, repo={repo}")

        can_launch, reason = await self._budget_manager.can_launch_agent()
        if not can_launch:
            logger.warning(f"Budget check failed: {reason}")
            return None

        existing = await self._db.get_execution_for_issue(issue_id)
        if existing and existing.status in (ExecutionStatus.PENDING, ExecutionStatus.RUNNING):
            logger.info(f"Execution already active for issue {issue_id}")
            return None

        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
        except Exception as e:
            logger.error(f"Failed to get issue {issue_id} from {repo}: {e}")
            return None

        prompt = build_prompt(issue, repo, mode="implement")

        repo_url = f"https://github.com/{repo}.git"
        config = ExecutionConfig(
            repo_url=repo_url,
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

        execution = AgentExecution(
            id=execution_id,
            repo_url=repo_url,
            status=ExecutionStatus.PENDING,
            prompt=prompt,
            started_at=utc_now(),
        )
        claimed = await self._db.try_claim_issue(execution, issue_id=issue_id)
        if not claimed:
            logger.info(f"Lost claim race for issue {issue_id}")
            return None
        logger.info(f"Launched agent {execution_id} for issue {issue_id}")

        return execution_id

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
