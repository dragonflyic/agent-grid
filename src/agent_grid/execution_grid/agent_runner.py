"""Agent runner using Claude Code SDK."""

import asyncio
from pathlib import Path
from uuid import UUID

from claude_code_sdk import query
from claude_code_sdk.types import (
    ClaudeCodeOptions,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)

from .public_api import AgentExecution, ExecutionStatus, utc_now
from ..config import settings
from .event_publisher import event_publisher
from .repo_manager import get_repo_manager


# Testing override: appended to prompt when test_force_planning_only is enabled
# Instructions vary based on issue tracker type

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


class AgentRunner:
    """
    Runs coding agents using the Claude Code SDK.

    Each agent gets a fresh repo clone and works in isolation.
    """

    def __init__(self):
        self._active_executions: dict[UUID, AgentExecution] = {}
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._repo_manager = get_repo_manager()

    async def run(
        self,
        execution: AgentExecution,
        prompt: str,
    ) -> AgentExecution:
        """
        Run an agent for the given execution.

        Args:
            execution: The execution record.
            prompt: The prompt to send to the agent.

        Returns:
            Updated execution with results.
        """
        execution_id = execution.id
        self._active_executions[execution_id] = execution

        try:
            # Clone repository
            work_dir = await self._repo_manager.clone_repo(
                execution_id,
                execution.repo_url,
            )

            # Create working branch
            branch_name = f"agent/{execution.issue_id}"
            await self._repo_manager.create_branch(execution_id, branch_name)

            # Update status to running
            execution.status = ExecutionStatus.RUNNING
            execution.started_at = utc_now()

            # Publish started event
            await event_publisher.agent_started(
                execution_id,
                execution.issue_id,
                execution.repo_url,
            )

            # Run the agent
            result = await self._run_agent(execution_id, work_dir, prompt, execution)

            # Push changes if any
            try:
                await self._repo_manager.push_branch(execution_id, branch_name)
            except RuntimeError:
                # No changes to push is okay
                pass

            # Update execution
            execution.status = ExecutionStatus.COMPLETED
            execution.completed_at = utc_now()
            execution.result = result

            # Publish completed event
            await event_publisher.agent_completed(execution_id, result)

            # Cleanup if configured
            if settings.cleanup_on_success:
                await self._repo_manager.cleanup(execution_id)

        except Exception as e:
            execution.status = ExecutionStatus.FAILED
            execution.completed_at = utc_now()
            execution.result = str(e)

            # Publish failed event
            await event_publisher.agent_failed(execution_id, str(e))

            # Cleanup if configured
            if settings.cleanup_on_failure:
                await self._repo_manager.cleanup(execution_id)

        finally:
            # Remove from active executions
            self._active_executions.pop(execution_id, None)
            self._tasks.pop(execution_id, None)

        return execution

    async def _run_agent(
        self,
        execution_id: UUID,
        work_dir: Path,
        prompt: str,
        execution: AgentExecution,
    ) -> str:
        """
        Run the Claude Code SDK agent.

        Args:
            execution_id: Execution ID for tracking.
            work_dir: Working directory for the agent.
            prompt: The prompt to send.
            execution: The execution record (for repo/issue context).

        Returns:
            The agent's final output.
        """
        # Determine permission mode - autonomous agents need bypassPermissions
        # since there's no human to approve bash commands
        permission_mode = "bypassPermissions" if settings.agent_bypass_permissions else "acceptEdits"

        # Apply testing override if configured
        if settings.test_force_planning_only:
            # Extract repo from repo_url (e.g., "https://github.com/owner/repo.git" -> "owner/repo")
            repo = self._extract_repo_from_url(execution.repo_url)
            prompt = prompt + get_planning_override(repo, execution.issue_id)

        options = ClaudeCodeOptions(
            cwd=work_dir,
            permission_mode=permission_mode,
        )

        # Collect output
        output_parts: list[str] = []
        final_result: str | None = None

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, SystemMessage):
                # System messages (e.g., from Claude's initialization)
                await event_publisher.agent_chat(
                    execution_id,
                    message_type="system",
                    content=message.subtype if hasattr(message, "subtype") else "system",
                )
            elif isinstance(message, UserMessage):
                # User messages (typically tool results being fed back)
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        # Truncate large tool results for logging
                        content = block.content if isinstance(block.content, str) else str(block.content)
                        if len(content) > 1000:
                            content = content[:1000] + "... [truncated]"
                        await event_publisher.agent_chat(
                            execution_id,
                            message_type="tool_result",
                            content=content,
                            tool_id=block.tool_use_id,
                        )
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        output_parts.append(block.text)
                        # Stream full text content
                        await event_publisher.agent_chat(
                            execution_id,
                            message_type="text",
                            content=block.text,
                        )
                        # Also emit progress event (truncated) for backward compat
                        await event_publisher.agent_progress(
                            execution_id,
                            block.text[:200],
                            "text",
                        )
                    elif isinstance(block, ToolUseBlock):
                        # Include tool input in the log
                        import json
                        tool_input = json.dumps(block.input, indent=2) if block.input else ""
                        await event_publisher.agent_chat(
                            execution_id,
                            message_type="tool_use",
                            content=tool_input,
                            tool_name=block.name,
                            tool_id=block.id,
                        )
                        await event_publisher.agent_progress(
                            execution_id,
                            f"Using tool: {block.name}",
                            "tool",
                        )
            elif isinstance(message, ResultMessage):
                if message.result:
                    final_result = message.result
                    await event_publisher.agent_chat(
                        execution_id,
                        message_type="result",
                        content=message.result,
                    )

        return final_result or "\n".join(output_parts)

    def start_execution(self, execution: AgentExecution, prompt: str) -> None:
        """
        Start an execution in the background.

        Args:
            execution: The execution record.
            prompt: The prompt to send to the agent.
        """
        task = asyncio.create_task(self.run(execution, prompt))
        self._tasks[execution.id] = task

    def get_execution(self, execution_id: UUID) -> AgentExecution | None:
        """Get an active execution by ID."""
        return self._active_executions.get(execution_id)

    def get_active_executions(self) -> list[AgentExecution]:
        """Get all active executions."""
        return list(self._active_executions.values())

    async def cancel_execution(self, execution_id: UUID) -> bool:
        """
        Cancel an active execution.

        Args:
            execution_id: ID of the execution to cancel.

        Returns:
            True if cancelled, False if not found.
        """
        task = self._tasks.get(execution_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Update execution status
            execution = self._active_executions.get(execution_id)
            if execution:
                execution.status = ExecutionStatus.FAILED
                execution.completed_at = utc_now()
                execution.result = "Cancelled"
                await event_publisher.agent_failed(execution_id, "Cancelled")

            return True
        return False

    def _extract_repo_from_url(self, repo_url: str) -> str:
        """Extract owner/repo from a git URL."""
        # Handle https://github.com/owner/repo.git
        if "github.com" in repo_url:
            parts = repo_url.replace(".git", "").split("github.com/")
            if len(parts) > 1:
                return parts[1]
        return "unknown/repo"


# Global instance
_agent_runner: AgentRunner | None = None


def get_agent_runner() -> AgentRunner:
    """Get the global agent runner instance."""
    global _agent_runner
    if _agent_runner is None:
        _agent_runner = AgentRunner()
    return _agent_runner
