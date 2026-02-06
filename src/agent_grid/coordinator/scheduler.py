"""Scheduler for deciding when to launch agents."""

import logging
from uuid import UUID

from ..config import settings
from ..execution_grid import (
    event_bus,
    get_execution_grid,
    AgentExecution,
    Event,
    EventType,
    ExecutionConfig,
    ExecutionStatus,
)

logger = logging.getLogger("agent_grid.scheduler")
from ..issue_tracker import get_issue_tracker
from .budget_manager import get_budget_manager
from .database import get_database
from .nudge_handler import get_nudge_handler


# Planning mode instructions - appended to prompt when test_force_planning_only is enabled
PLANNING_ONLY_HEADER = """

---

**TESTING OVERRIDE - PLANNING ONLY MODE**

CRITICAL: You are running in PLANNING ONLY mode for testing. You must:

1. DO NOT write ANY code
2. DO NOT create or edit any files in the repository
3. DO NOT make any commits
4. Your ONLY task is to create subissues

Break down the issue into 2-5 smaller, independently implementable subissues.
Include the "agent" label so subissues are automatically picked up.

Remember: DO NOT write code. DO NOT edit files. ONLY create subissues.
"""

PLANNING_GITHUB_INSTRUCTIONS = """
## How to Create Subissues (GitHub)

Use the `gh` CLI to create issues and add them as sub-issues:

```bash
# 1. Create the issue and capture its URL
ISSUE_URL=$(gh issue create --repo {repo} \\
  --title "Subissue title" \\
  --body "Description with acceptance criteria" \\
  --label "agent" 2>&1 | tail -1)

# 2. Extract the issue number from the URL
ISSUE_NUM=$(echo "$ISSUE_URL" | grep -oE '[0-9]+$')

# 3. Get the issue's internal ID (required by sub-issues API)
ISSUE_ID=$(gh api repos/{repo}/issues/$ISSUE_NUM --jq '.id')

# 4. Add it as a sub-issue to the parent
gh api -X POST repos/{repo}/issues/{issue_id}/sub_issues -f sub_issue_id="$ISSUE_ID"
```

Or as a one-liner for each subissue:
```bash
ISSUE_NUM=$(gh issue create --repo {repo} --title "Title" --body "Body" --label "agent" 2>&1 | grep -oE '[0-9]+$') && \\
ISSUE_ID=$(gh api repos/{repo}/issues/$ISSUE_NUM --jq '.id') && \\
gh api -X POST repos/{repo}/issues/{issue_id}/sub_issues --input - <<< "{{\\"sub_issue_id\\": $ISSUE_ID}}"
```
"""

PLANNING_FILESYSTEM_INSTRUCTIONS = """
## How to Create Subissues (Local Testing)

Use curl to create subissues via the Agent Grid API:

```bash
curl -X POST "http://localhost:8000/api/issues/{repo}/{issue_id}/subissues" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "title": "Subissue title here",
    "body": "Description with acceptance criteria",
    "labels": ["agent"]
  }}'
```
"""


def get_planning_override(repo: str, issue_id: str) -> str:
    """Get the planning-only override prompt based on issue tracker type."""
    if settings.issue_tracker_type == "github":
        instructions = PLANNING_GITHUB_INSTRUCTIONS.format(repo=repo, issue_id=issue_id)
    else:
        instructions = PLANNING_FILESYSTEM_INSTRUCTIONS.format(repo=repo, issue_id=issue_id)

    return PLANNING_ONLY_HEADER + instructions


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
        logger.info(f"Handling nudge request: issue_id={issue_id}, repo={repo}")

        if not issue_id:
            logger.warning("Nudge request missing issue_id")
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
        logger.info(f"Attempting to launch agent: issue_id={issue_id}, repo={repo}")

        # Check budget
        can_launch, reason = await self._budget_manager.can_launch_agent()
        if not can_launch:
            logger.warning(f"Budget check failed: {reason}")
            return None

        # Check if already running for this issue
        existing = await self._db.get_execution_for_issue(issue_id)
        if existing and existing.status == ExecutionStatus.RUNNING:
            logger.info(f"Execution already running for issue {issue_id}")
            return None

        # Get issue details
        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
        except Exception as e:
            logger.error(f"Failed to get issue {issue_id} from {repo}: {e}")
            return None

        # Generate prompt with all instructions
        prompt = self._generate_prompt(issue.title, issue.body or "", issue_id, repo)

        # Apply testing override if configured
        if settings.test_force_planning_only:
            prompt = prompt + get_planning_override(repo, issue_id)

        # Build execution config
        repo_url = f"https://github.com/{repo}.git"
        permission_mode = "bypassPermissions" if settings.agent_bypass_permissions else "acceptEdits"
        config = ExecutionConfig(
            repo_url=repo_url,
            prompt=prompt,
            permission_mode=permission_mode,
        )

        # Launch agent
        grid = get_execution_grid()
        execution_id = await grid.launch_agent(config)
        logger.info(f"Launched agent {execution_id} for issue {issue_id}")

        # Record in database (coordinator tracks issue_id mapping internally)
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
        trigger_labels = {"agent", "automated", "agent-grid"}
        return bool(set(labels) & trigger_labels)

    def _generate_prompt(self, title: str, body: str, issue_id: str, repo: str) -> str:
        """Generate the prompt for the agent."""
        branch_name = f"agent/{issue_id}"
        return f"""You are working on the following issue:

## {title}

{body}

## Setup

First, create and checkout a working branch:
```bash
git checkout -b {branch_name}
```

## Instructions

Please analyze this issue and implement the necessary changes. When you're done:
1. Commit your changes with a clear commit message
2. Push your branch to the remote:
   ```bash
   git push -u origin {branch_name}
   ```
3. Create a pull request for your branch targeting the main branch
   - In the PR description, reference the initiating issue by including "Closes #{issue_id}" or "Fixes #{issue_id}"
   - After creating the PR, link it to the issue by running: `gh pr edit --add-issue #{issue_id}`
4. If you need help from another agent on a related issue, use the nudge endpoint

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
