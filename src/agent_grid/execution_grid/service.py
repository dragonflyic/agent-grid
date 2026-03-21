"""Implementation of the ExecutionGrid service."""

from typing import TYPE_CHECKING, Awaitable, Callable
from uuid import UUID, uuid4

from ..config import settings
from .agent_runner import get_agent_runner
from .event_bus import event_bus
from .public_api import (
    AgentEventHandler,
    AgentExecution,
    Event,
    ExecutionConfig,
    ExecutionGrid,
)

if TYPE_CHECKING:
    from .claude_code_grid import ClaudeCodeExecutionGrid
    from .fly_grid import FlyExecutionGrid


class ExecutionGridService(ExecutionGrid):
    """
    Implementation of the ExecutionGrid interface.

    Coordinates agent execution and event subscriptions.
    """

    def __init__(self):
        self._agent_runner = get_agent_runner()
        # Map handler IDs to internal event handlers for cleanup
        self._handler_mapping: dict[AgentEventHandler, Callable[[Event], Awaitable[None]]] = {}

    async def launch_agent(
        self,
        config: ExecutionConfig,
        mode: str = "implement",
        issue_number: int | None = None,
        context: dict | None = None,
        execution_id: UUID | None = None,
    ) -> UUID:
        """Launch a generic Claude Code session."""
        execution_id = execution_id or uuid4()
        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            prompt=config.prompt,
        )
        self._agent_runner.start_execution(execution, config)
        return execution_id

    async def get_execution_status(self, execution_id: UUID) -> AgentExecution | None:
        """Get the status of an execution."""
        return self._agent_runner.get_execution(execution_id)

    def get_active_executions(self) -> list[AgentExecution]:
        """Get all active executions."""
        return self._agent_runner.get_active_executions()

    async def cancel_execution(self, execution_id: UUID) -> bool:
        """Cancel an active execution."""
        return await self._agent_runner.cancel_execution(execution_id)

    def subscribe_to_agent_events(self, handler: AgentEventHandler) -> None:
        """
        Subscribe to all agent execution events.

        The handler is called with (event_type: str, payload: dict).
        """

        async def event_handler(event: Event) -> None:
            await handler(event.type.value, event.payload)

        # Store mapping for later unsubscription
        self._handler_mapping[handler] = event_handler
        event_bus.subscribe(event_handler, event_type=None)

    def unsubscribe_from_agent_events(self, handler: AgentEventHandler) -> None:
        """Unsubscribe from agent events."""
        event_handler = self._handler_mapping.pop(handler, None)
        if event_handler:
            event_bus.unsubscribe(event_handler, event_type=None)


# Global service instances
_service: ExecutionGridService | None = None
_fly_grid: "FlyExecutionGrid | None" = None
_claude_code_grid: "ClaudeCodeExecutionGrid | None" = None


def get_execution_grid() -> ExecutionGrid:
    """
    Get the global execution grid service instance.

    Returns different implementations based on deployment_mode and execution_backend:
    - deployment_mode="local": In-memory service that runs agents directly
    - deployment_mode="coordinator" + execution_backend="claude-code": Claude Code CLI on Fly
    - deployment_mode="coordinator" + execution_backend="fly": Fly Machines workers
    """
    global _service, _fly_grid, _claude_code_grid

    if settings.deployment_mode == "coordinator":
        if settings.execution_backend == "claude-code":
            if _claude_code_grid is None:
                from .claude_code_grid import get_claude_code_execution_grid

                _claude_code_grid = get_claude_code_execution_grid()
            return _claude_code_grid
        else:
            # Fly Machines-based implementation
            if _fly_grid is None:
                from .fly_grid import FlyExecutionGrid

                _fly_grid = FlyExecutionGrid()
            return _fly_grid
    else:
        # Default: local in-memory implementation
        if _service is None:
            _service = ExecutionGridService()
        return _service
