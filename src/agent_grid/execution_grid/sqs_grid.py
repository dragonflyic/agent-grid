"""SQS-based ExecutionGrid implementation for coordinator deployment.

This implementation publishes job requests to SQS and listens for results,
without running agents directly. Agents are executed by the local worker.
"""

import asyncio
import logging
from typing import Callable, Awaitable
from uuid import UUID, uuid4

from .public_api import (
    AgentExecution,
    AgentEventHandler,
    Event,
    EventType,
    ExecutionConfig,
    ExecutionGrid,
    ExecutionStatus,
    utc_now,
)
from .sqs_client import get_sqs_client, JobRequest, JobResult
from .event_bus import event_bus
from ..config import settings

logger = logging.getLogger("agent_grid.sqs_grid")


class ExecutionGridClient(ExecutionGrid):
    """
    SQS-based execution grid for coordinator deployment.

    - Publishes job requests to SQS (picked up by local worker)
    - Listens for results from result queue
    - Updates local state and publishes events

    Does NOT run agents directly - that's the worker's job.
    """

    def __init__(self):
        self._sqs = get_sqs_client()
        self._executions: dict[UUID, AgentExecution] = {}
        self._handler_mapping: dict[int, Callable[[Event], Awaitable[None]]] = {}
        self._result_listener_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the result queue listener."""
        if self._running:
            return

        self._running = True
        self._result_listener_task = asyncio.create_task(self._result_listener_loop())
        logger.info("Started SQS result listener")

    async def stop(self) -> None:
        """Stop the result queue listener."""
        self._running = False
        if self._result_listener_task:
            self._result_listener_task.cancel()
            try:
                await self._result_listener_task
            except asyncio.CancelledError:
                pass
            self._result_listener_task = None
        logger.info("Stopped SQS result listener")

    async def _result_listener_loop(self) -> None:
        """Background loop that polls for job results."""
        while self._running:
            try:
                results = await self._sqs.poll_job_results(
                    max_messages=10,
                    wait_time_seconds=5,
                )

                for result, receipt_handle in results:
                    await self._handle_result(result)
                    await self._sqs.delete_job_result(receipt_handle)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in result listener: {e}")
                await asyncio.sleep(5)

    async def _handle_result(self, result: JobResult) -> None:
        """Handle a job result from the worker."""
        execution_id = UUID(result.execution_id)
        execution = self._executions.get(execution_id)

        if not execution:
            logger.warning(f"Received result for unknown execution {execution_id}")
            # Still publish the event - database handler will catch it
            if result.status == "completed":
                await event_bus.publish(
                    EventType.AGENT_COMPLETED,
                    {
                        "execution_id": str(execution_id),
                        "result": result.result,
                        "branch": result.branch,
                    },
                )
            else:
                await event_bus.publish(
                    EventType.AGENT_FAILED,
                    {
                        "execution_id": str(execution_id),
                        "error": result.result,
                    },
                )
            return

        # Update local execution state
        execution.completed_at = result.completed_at
        execution.result = result.result

        if result.status == "completed":
            execution.status = ExecutionStatus.COMPLETED
            await event_bus.publish(
                EventType.AGENT_COMPLETED,
                {
                    "execution_id": str(execution_id),
                    "result": result.result,
                    "branch": result.branch,
                },
            )
            logger.info(f"Execution {execution_id} completed")
        else:
            execution.status = ExecutionStatus.FAILED
            await event_bus.publish(
                EventType.AGENT_FAILED,
                {
                    "execution_id": str(execution_id),
                    "error": result.result,
                },
            )
            logger.info(f"Execution {execution_id} failed: {result.result}")

        # Remove from active tracking
        self._executions.pop(execution_id, None)

    async def launch_agent(self, config: ExecutionConfig) -> UUID:
        """
        Launch a generic Claude Code session.

        In SQS mode, this publishes a job request to the queue.
        The actual execution happens on the local worker.
        """
        execution_id = uuid4()

        # Create local tracking record
        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=config.prompt,
        )
        self._executions[execution_id] = execution

        # Publish job request to SQS
        job_request = JobRequest(
            execution_id=str(execution_id),
            repo_url=config.repo_url,
            prompt=config.prompt,
            permission_mode=config.permission_mode,
            created_at=utc_now(),
        )

        try:
            await self._sqs.publish_job_request(job_request)
            logger.info(f"Published job request {execution_id}")

            # Publish started event (optimistic - worker may not pick it up immediately)
            await event_bus.publish(
                EventType.AGENT_STARTED,
                {
                    "execution_id": str(execution_id),
                    "repo_url": config.repo_url,
                },
            )

        except Exception as e:
            logger.error(f"Failed to publish job request: {e}")
            execution.status = ExecutionStatus.FAILED
            execution.result = f"Failed to queue job: {e}"
            execution.completed_at = utc_now()
            self._executions.pop(execution_id, None)
            raise

        return execution_id

    async def get_execution_status(self, execution_id: UUID) -> AgentExecution | None:
        """Get the status of an execution from local cache."""
        return self._executions.get(execution_id)

    def get_active_executions(self) -> list[AgentExecution]:
        """Get all active executions from local cache."""
        return list(self._executions.values())

    async def cancel_execution(self, execution_id: UUID) -> bool:
        """
        Cancel an active execution.

        Note: In SQS mode, we can only cancel if the job hasn't been
        picked up by the worker yet. Once it's running, it will complete.
        """
        execution = self._executions.get(execution_id)
        if not execution:
            return False

        if execution.status == ExecutionStatus.PENDING:
            # Could potentially delete from SQS, but message might already be in flight
            execution.status = ExecutionStatus.FAILED
            execution.completed_at = utc_now()
            execution.result = "Cancelled"

            await event_bus.publish(
                EventType.AGENT_FAILED,
                {
                    "execution_id": str(execution_id),
                    "error": "Cancelled",
                },
            )

            self._executions.pop(execution_id, None)
            return True

        # Job is already running on worker, can't cancel remotely
        logger.warning(f"Cannot cancel running execution {execution_id}")
        return False

    def subscribe_to_agent_events(self, handler: AgentEventHandler) -> None:
        """Subscribe to all agent execution events."""
        async def event_handler(event: Event) -> None:
            await handler(event.type.value, event.payload)

        self._handler_mapping[id(handler)] = event_handler
        event_bus.subscribe(event_handler, event_type=None)

    def unsubscribe_from_agent_events(self, handler: AgentEventHandler) -> None:
        """Unsubscribe from agent events."""
        event_handler = self._handler_mapping.pop(id(handler), None)
        if event_handler:
            event_bus.unsubscribe(event_handler, event_type=None)


# Global instance
_sqs_grid: ExecutionGridClient | None = None


def get_sqs_execution_grid() -> ExecutionGridClient:
    """Get the global SQS-based execution grid instance."""
    global _sqs_grid
    if _sqs_grid is None:
        _sqs_grid = ExecutionGridClient()
    return _sqs_grid
