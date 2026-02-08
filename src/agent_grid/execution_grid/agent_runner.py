"""Agent runner using Claude Code SDK."""

import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID

from claude_code_sdk import query
from claude_code_sdk.types import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..config import settings
from .event_publisher import event_publisher
from .public_api import AgentExecution, ExecutionConfig, ExecutionStatus, utc_now
from .repo_manager import get_repo_manager

logger = logging.getLogger("agent_grid.agent")


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
        config: ExecutionConfig,
    ) -> AgentExecution:
        """
        Run an agent for the given execution.

        Args:
            execution: The execution record.
            config: Configuration for the execution.

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

            # Update status to running
            execution.status = ExecutionStatus.RUNNING
            execution.started_at = utc_now()

            # Publish started event
            await event_publisher.agent_started(
                execution_id,
                execution.repo_url,
            )
            exec_id = str(execution_id)[:8]
            repo = execution.repo_url.replace("https://github.com/", "").replace(".git", "")
            logger.info(f"[{exec_id}] 🚀 STARTED - repo={repo}")

            # Run the agent
            result = await self._run_agent(execution_id, work_dir, config)

            # Update execution
            execution.status = ExecutionStatus.COMPLETED
            execution.completed_at = utc_now()
            execution.result = result

            # Publish completed event
            await event_publisher.agent_completed(execution_id, result)
            preview = result[:100].replace("\n", " ") if result else "(no result)"
            logger.info(f"[{exec_id}] ✅ COMPLETED - {preview}")

            # Cleanup if configured
            if settings.cleanup_on_success:
                await self._repo_manager.cleanup(execution_id)

        except Exception as e:
            execution.status = ExecutionStatus.FAILED
            execution.completed_at = utc_now()
            execution.result = str(e)

            # Publish failed event
            await event_publisher.agent_failed(execution_id, str(e))
            exec_id = str(execution_id)[:8]
            logger.error(f"[{exec_id}] ❌ FAILED - {e}")

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
        config: ExecutionConfig,
    ) -> str:
        """
        Run the Claude Code SDK agent.

        Args:
            execution_id: Execution ID for tracking.
            work_dir: Working directory for the agent.
            config: Configuration for the execution.

        Returns:
            The agent's final output.
        """
        options = ClaudeCodeOptions(
            cwd=work_dir,
            permission_mode=config.permission_mode,
        )

        # Collect output
        output_parts: list[str] = []
        final_result: str | None = None
        exec_id = str(execution_id)[:8]

        async for message in query(prompt=config.prompt, options=options):
            if isinstance(message, SystemMessage):
                # System messages (e.g., from Claude's initialization)
                subtype = message.subtype if hasattr(message, "subtype") else "system"
                await event_publisher.agent_chat(
                    execution_id,
                    message_type="system",
                    content=subtype,
                )
                logger.info(f"[{exec_id}] ⚙️  System: {subtype}")
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
                        preview = content[:150].replace("\n", " ")
                        if len(content) > 150:
                            preview += "..."
                        logger.info(f"[{exec_id}] 📎 Result: {preview}")
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
                        # Log text
                        for line in block.text.split("\n"):
                            if line.strip():
                                logger.info(f"[{exec_id}] 💬 {line}")
                    elif isinstance(block, ToolUseBlock):
                        # Include tool input in the log
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
                        logger.info(f"[{exec_id}] 🔧 Tool: {block.name}")
                        if tool_input and len(tool_input) < 200:
                            for line in tool_input.split("\n"):
                                logger.info(f"[{exec_id}]    {line}")
            elif isinstance(message, ResultMessage):
                if message.result:
                    final_result = message.result
                    await event_publisher.agent_chat(
                        execution_id,
                        message_type="result",
                        content=message.result,
                    )
                    logger.info(f"[{exec_id}] 🏁 Final result:")
                    for line in message.result.split("\n")[:10]:
                        if line.strip():
                            logger.info(f"[{exec_id}]    {line}")

        return final_result or "\n".join(output_parts)

    def start_execution(self, execution: AgentExecution, config: ExecutionConfig) -> None:
        """
        Start an execution in the background.

        Args:
            execution: The execution record.
            config: Configuration for the execution.
        """
        task = asyncio.create_task(self.run(execution, config))
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
