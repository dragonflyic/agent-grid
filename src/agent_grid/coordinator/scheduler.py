"""Scheduler for deciding when to launch agents."""

from uuid import UUID

from ..common import event_bus, Event, EventType
from ..common.models import ExecutionStatus, AgentExecution
from ..execution_grid import launch_agent
from ..issue_tracker import get_issue_tracker
from .budget_manager import get_budget_manager
from .database import get_database
from .nudge_handler import get_nudge_handler


class Scheduler:
    """
    Decides when to launch agents based on events.

    Listens for:
    - New issues created
    - Nudge requests
    - Execution completions
    """

    def __init__(self):
        self._db = get_database()
        self._budget_manager = get_budget_manager()
        self._nudge_handler = get_nudge_handler()
        self._running = False

    async def start(self) -> None:
        """Start the scheduler and subscribe to events."""
        self._running = True

        # Subscribe to relevant events
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

        # Check if this issue should trigger an agent
        if not self._should_auto_launch(labels):
            return

        await self._try_launch_agent(
            issue_id=issue_id,
            repo=repo,
        )

    async def _handle_nudge_requested(self, event: Event) -> None:
        """Handle nudge request."""
        payload = event.payload
        issue_id = payload.get("issue_id")
        repo = payload.get("repo")

        if not issue_id:
            return

        # If repo not provided, try to get it from nudge source
        if not repo:
            nudge_id = payload.get("nudge_id")
            if nudge_id:
                nudges = await self._nudge_handler.get_pending_nudges(limit=100)
                for nudge in nudges:
                    if str(nudge.id) == nudge_id and nudge.source_execution_id:
                        source_exec = await self._db.get_execution(nudge.source_execution_id)
                        if source_exec:
                            # Extract repo from repo_url
                            repo = self._extract_repo_from_url(source_exec.repo_url)
                            break

        if repo:
            await self._try_launch_agent(issue_id=issue_id, repo=repo)

    async def _handle_agent_completed(self, event: Event) -> None:
        """Handle agent completion."""
        payload = event.payload
        execution_id = payload.get("execution_id")

        if execution_id:
            # Update database
            execution = await self._db.get_execution(UUID(execution_id))
            if execution:
                execution.status = ExecutionStatus.COMPLETED
                execution.result = payload.get("result")
                await self._db.update_execution(execution)

        # Process any pending nudges now that we have capacity
        await self._process_pending_nudges()

    async def _handle_agent_failed(self, event: Event) -> None:
        """Handle agent failure."""
        payload = event.payload
        execution_id = payload.get("execution_id")

        if execution_id:
            execution = await self._db.get_execution(UUID(execution_id))
            if execution:
                execution.status = ExecutionStatus.FAILED
                execution.result = payload.get("error")
                await self._db.update_execution(execution)

        # Process pending nudges
        await self._process_pending_nudges()

    async def _try_launch_agent(self, issue_id: str, repo: str) -> UUID | None:
        """
        Attempt to launch an agent for an issue.

        Args:
            issue_id: The issue ID.
            repo: Repository in owner/name format.

        Returns:
            Execution ID if launched, None otherwise.
        """
        # Check budget
        can_launch, reason = await self._budget_manager.can_launch_agent()
        if not can_launch:
            return None

        # Check if already running for this issue
        existing = await self._db.get_execution_for_issue(issue_id)
        if existing and existing.status == ExecutionStatus.RUNNING:
            return None

        # Get issue details
        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
        except Exception:
            return None

        # Generate prompt
        prompt = self._generate_prompt(issue.title, issue.body or "")

        # Launch agent
        repo_url = f"https://github.com/{repo}.git"
        execution_id = await launch_agent(issue_id, repo_url, prompt)

        # Record in database
        execution = AgentExecution(
            id=execution_id,
            issue_id=issue_id,
            repo_url=repo_url,
            status=ExecutionStatus.PENDING,
            prompt=prompt,
        )
        await self._db.create_execution(execution)

        return execution_id

    async def _process_pending_nudges(self) -> None:
        """Process pending nudge requests."""
        nudges = await self._nudge_handler.get_pending_nudges(limit=5)

        for nudge in nudges:
            # Try to get repo from source execution
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
        # Auto-launch for issues with 'agent' or 'automated' labels
        trigger_labels = {"agent", "automated", "agent-grid"}
        return bool(set(labels) & trigger_labels)

    def _generate_prompt(self, title: str, body: str) -> str:
        """Generate the prompt for the agent."""
        return f"""You are working on the following issue:

## {title}

{body}

Please analyze this issue and implement the necessary changes. When you're done:
1. Commit your changes with a clear commit message
2. If you need help from another agent on a related issue, use the nudge endpoint

Work carefully and test your changes before committing."""

    def _extract_repo_from_url(self, repo_url: str) -> str | None:
        """Extract owner/repo from a git URL."""
        # Handle https://github.com/owner/repo.git
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
