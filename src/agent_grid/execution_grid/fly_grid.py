"""Fly.io-based ExecutionGrid implementation for coordinator deployment.

Replaces SQS-based grid. Spawns ephemeral Fly Machines per execution.
Results come back via HTTP callback to /api/agent-status.
"""

import json
import logging
from typing import Awaitable, Callable
from uuid import UUID, uuid4

from ..fly import get_fly_client
from .event_bus import event_bus
from .public_api import (
    AgentEventHandler,
    AgentExecution,
    Event,
    EventType,
    ExecutionConfig,
    ExecutionGrid,
    ExecutionStatus,
    utc_now,
)

logger = logging.getLogger("agent_grid.fly_grid")


class FlyExecutionGrid(ExecutionGrid):
    """Fly Machines-based execution grid.

    - Spawns ephemeral Fly Machines for each execution
    - Machines POST results back to /api/agent-status
    - Orchestrator polls Fly API as fallback for stale machines
    """

    def __init__(self):
        self._fly = get_fly_client()
        self._executions: dict[UUID, AgentExecution] = {}
        self._machine_map: dict[UUID, str] = {}  # execution_id -> machine_id
        self._handler_mapping: dict[int, Callable[[Event], Awaitable[None]]] = {}

    async def launch_agent(
        self,
        config: ExecutionConfig,
        mode: str = "implement",
        issue_number: int | None = None,
        context: dict | None = None,
        execution_id: UUID | None = None,
    ) -> UUID:
        """Launch an agent on an ephemeral Fly Machine."""
        execution_id = execution_id or uuid4()

        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=config.prompt,
        )
        self._executions[execution_id] = execution

        try:
            machine = await self._fly.spawn_worker(
                execution_id=str(execution_id),
                repo_url=config.repo_url,
                issue_number=issue_number or 0,
                prompt=config.prompt,
                mode=mode,
                context_json=json.dumps(context or {}),
            )
            self._machine_map[execution_id] = machine["id"]

            await event_bus.publish(
                EventType.AGENT_STARTED,
                {
                    "execution_id": str(execution_id),
                    "repo_url": config.repo_url,
                    "machine_id": machine["id"],
                },
            )
            logger.info(f"Launched Fly Machine {machine['id']} for execution {execution_id}")

        except Exception as e:
            logger.error(f"Failed to spawn Fly Machine: {e}")
            execution.status = ExecutionStatus.FAILED
            execution.result = f"Failed to spawn worker: {e}"
            execution.completed_at = utc_now()
            self._executions.pop(execution_id, None)
            raise

        return execution_id

    async def handle_agent_result(
        self,
        execution_id: UUID,
        status: str,
        result: str | None = None,
        branch: str | None = None,
        pr_number: int | None = None,
        checkpoint: dict | None = None,
    ) -> None:
        """Handle a result callback from a Fly Machine worker.

        Called by the /api/agent-status endpoint.
        """
        execution = self._executions.get(execution_id)

        if status == "completed":
            if execution:
                execution.status = ExecutionStatus.COMPLETED
                execution.completed_at = utc_now()
                execution.result = result

            await event_bus.publish(
                EventType.AGENT_COMPLETED,
                {
                    "execution_id": str(execution_id),
                    "result": result,
                    "branch": branch,
                    "pr_number": pr_number,
                    "checkpoint": checkpoint,
                },
            )
            logger.info(f"Execution {execution_id} completed")
        else:
            if execution:
                execution.status = ExecutionStatus.FAILED
                execution.completed_at = utc_now()
                execution.result = result

            await event_bus.publish(
                EventType.AGENT_FAILED,
                {
                    "execution_id": str(execution_id),
                    "error": result,
                },
            )
            logger.info(f"Execution {execution_id} failed: {result}")

        self._executions.pop(execution_id, None)
        self._machine_map.pop(execution_id, None)

    async def get_execution_status(self, execution_id: UUID) -> AgentExecution | None:
        return self._executions.get(execution_id)

    def get_active_executions(self) -> list[AgentExecution]:
        return list(self._executions.values())

    async def cancel_execution(self, execution_id: UUID) -> bool:
        machine_id = self._machine_map.get(execution_id)
        if machine_id:
            await self._fly.destroy_machine(machine_id)
            execution = self._executions.get(execution_id)
            if execution:
                execution.status = ExecutionStatus.FAILED
                execution.completed_at = utc_now()
                execution.result = "Cancelled"
            await event_bus.publish(
                EventType.AGENT_FAILED,
                {"execution_id": str(execution_id), "error": "Cancelled"},
            )
            self._executions.pop(execution_id, None)
            self._machine_map.pop(execution_id, None)
            return True
        return False

    def subscribe_to_agent_events(self, handler: AgentEventHandler) -> None:
        async def event_handler(event: Event) -> None:
            await handler(event.type.value, event.payload)

        self._handler_mapping[id(handler)] = event_handler
        event_bus.subscribe(event_handler, event_type=None)

    def unsubscribe_from_agent_events(self, handler: AgentEventHandler) -> None:
        event_handler = self._handler_mapping.pop(id(handler), None)
        if event_handler:
            event_bus.unsubscribe(event_handler, event_type=None)


_fly_grid: FlyExecutionGrid | None = None


def get_fly_execution_grid() -> FlyExecutionGrid:
    global _fly_grid
    if _fly_grid is None:
        _fly_grid = FlyExecutionGrid()
    return _fly_grid
