"""Agent runner using Claude Code SDK."""

import asyncio
from pathlib import Path
from uuid import UUID

from claude_code_sdk import query
from claude_code_sdk.types import (
    ClaudeCodeOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from ..common.models import AgentExecution, ExecutionStatus, utc_now
from ..config import settings
from .event_publisher import event_publisher
from .repo_manager import get_repo_manager


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
            result = await self._run_agent(execution_id, work_dir, prompt)

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
    ) -> str:
        """
        Run the Claude Code SDK agent.

        Args:
            execution_id: Execution ID for tracking.
            work_dir: Working directory for the agent.
            prompt: The prompt to send.

        Returns:
            The agent's final output.
        """
        options = ClaudeCodeOptions(
            cwd=work_dir,
            permission_mode="acceptEdits",
        )

        # Collect output
        output_parts: list[str] = []
        final_result: str | None = None

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        output_parts.append(block.text)
                        await event_publisher.agent_progress(
                            execution_id,
                            block.text[:200],  # Truncate for progress
                            "text",
                        )
                    elif isinstance(block, ToolUseBlock):
                        await event_publisher.agent_progress(
                            execution_id,
                            f"Using tool: {block.name}",
                            "tool",
                        )
            elif isinstance(message, ResultMessage):
                if message.result:
                    final_result = message.result

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


# Global instance
_agent_runner: AgentRunner | None = None


def get_agent_runner() -> AgentRunner:
    """Get the global agent runner instance."""
    global _agent_runner
    if _agent_runner is None:
        _agent_runner = AgentRunner()
    return _agent_runner
