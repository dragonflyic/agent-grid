"""Scheduler for event-driven agent reactions.

The management loop now handles the cron-based scanning/classification/launching
pipeline. The scheduler focuses on real-time event-driven reactions:
- Webhook-triggered issue creation → immediate agent launch
- Nudge requests → immediate agent launch
- Agent completion → save checkpoint, update labels
- Agent failure → update labels
"""

import logging
from uuid import UUID

from ..execution_grid import (
    AgentExecution,
    Event,
    EventType,
    ExecutionConfig,
    ExecutionStatus,
    event_bus,
    get_execution_grid,
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
    - New issues created (webhook)
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

        if event.type == EventType.ISSUE_CREATED:
            await self._handle_issue_created(event)
        elif event.type == EventType.NUDGE_REQUESTED:
            await self._handle_nudge_requested(event)
        elif event.type == EventType.AGENT_COMPLETED:
            await self._handle_agent_completed(event)
        elif event.type == EventType.AGENT_FAILED:
            await self._handle_agent_failed(event)

    async def _handle_issue_created(self, event: Event) -> None:
        """Handle new issue creation."""
        payload = event.payload
        issue_id = payload.get("issue_id")
        repo = payload.get("repo")
        labels = payload.get("labels", [])

        if not self._should_auto_launch(labels):
            return

        await self._try_launch_agent(issue_id=issue_id, repo=repo)

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
                    await self._db.save_checkpoint(issue_id, checkpoint)

                # Update label to review-pending
                if issue_id:
                    repo = self._extract_repo_from_url(execution.repo_url)
                    if repo:
                        labels = get_label_manager()
                        await labels.transition_to(repo, issue_id, "ag/review-pending")

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
                        labels = get_label_manager()
                        await labels.transition_to(repo, issue_id, "ag/failed")

        await self._process_pending_nudges()

    async def _try_launch_agent(self, issue_id: str, repo: str) -> UUID | None:
        """Attempt to launch an agent for an issue."""
        logger.info(f"Attempting to launch agent: issue_id={issue_id}, repo={repo}")

        can_launch, reason = await self._budget_manager.can_launch_agent()
        if not can_launch:
            logger.warning(f"Budget check failed: {reason}")
            return None

        existing = await self._db.get_execution_for_issue(issue_id)
        if existing and existing.status == ExecutionStatus.RUNNING:
            logger.info(f"Execution already running for issue {issue_id}")
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
        logger.info(f"Launched agent {execution_id} for issue {issue_id}")

        execution = AgentExecution(
            id=execution_id,
            repo_url=repo_url,
            status=ExecutionStatus.PENDING,
            prompt=prompt,
        )
        await self._db.create_execution(execution, issue_id=issue_id)

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
