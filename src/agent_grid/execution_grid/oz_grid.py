"""Warp Oz-based ExecutionGrid implementation.

Replaces Fly Machines-based grid. Submits agent runs to Oz via the SDK
and polls for completion instead of relying on HTTP callbacks.

Layer discipline: this module does NOT import from coordinator or issue_tracker.
All DB persistence and fallback PR detection are handled via injectable callbacks
wired during application startup.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import UUID, uuid4

from ..config import settings
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

logger = logging.getLogger("agent_grid.oz_grid")

# Oz run states that indicate completion
_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


# ---------------------------------------------------------------------------
# Callback type definitions
# ---------------------------------------------------------------------------


@dataclass
class RunArtifacts:
    """PR artifacts extracted from a completed Oz run."""

    branch: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    result: str | None = None
    cost_cents: int | None = None


@dataclass
class OzCallbacks:
    """Injectable callbacks for cross-layer operations.

    Set via `OzExecutionGrid.set_callbacks()` during startup.
    All callbacks are optional — if not set, the operation is skipped.
    """

    # Called after a run is created: (execution_id, oz_run_id, session_link)
    on_run_created: Callable[[UUID, str, str | None], Awaitable[None]] | None = None

    # Called to recover in-flight runs on startup: () -> list[(exec_id, run_id)]
    recover_runs: Callable[[], Awaitable[list[tuple[UUID, str]]]] | None = None

    # Called when a run succeeds: (exec_id, artifacts) -> updated artifacts
    on_run_succeeded: Callable[[UUID, RunArtifacts], Awaitable[RunArtifacts]] | None = None

    # Called when a run fails: (exec_id, error_message)
    on_run_failed: Callable[[UUID, str], Awaitable[None]] | None = None


class OzExecutionGrid(ExecutionGrid):
    """Warp Oz-based execution grid.

    - Submits agent runs via the Oz Python SDK
    - Polls for run completion (no callback needed)
    - Extracts PR artifacts (branch, url) from completed runs
    """

    def __init__(self):
        from oz_agent_sdk import AsyncOzAPI

        self._client = AsyncOzAPI(api_key=settings.warp_api_key or None)
        self._executions: dict[UUID, AgentExecution] = {}
        self._run_map: dict[UUID, str] = {}  # execution_id -> oz_run_id
        self._handler_mapping: dict[AgentEventHandler, Callable[[Event], Awaitable[None]]] = {}
        self._poll_task: asyncio.Task | None = None
        self._polling = False
        self._poll_errors: dict[UUID, int] = {}  # consecutive poll failures per execution
        self._callbacks = OzCallbacks()

    def set_callbacks(self, callbacks: OzCallbacks) -> None:
        """Set callbacks for cross-layer operations (DB, PR detection, etc)."""
        self._callbacks = callbacks

    async def launch_agent(
        self,
        config: ExecutionConfig,
        mode: str = "implement",
        issue_number: int | None = None,
        context: dict | None = None,
        execution_id: UUID | None = None,
    ) -> UUID:
        """Launch an agent as an Oz cloud run."""
        execution_id = execution_id or uuid4()

        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=config.prompt,
        )
        self._executions[execution_id] = execution

        try:
            oz_config: dict = {}
            if settings.oz_environment_id:
                oz_config["environment_id"] = settings.oz_environment_id
            if settings.oz_model_id:
                oz_config["api_model_id"] = settings.oz_model_id

            title = f"Issue #{issue_number} ({mode})" if issue_number else f"Agent run ({mode})"

            response = await self._client.agent.run(
                prompt=config.prompt,
                config=oz_config if oz_config else None,
                title=title,
            )

            self._run_map[execution_id] = response.run_id

            # Persist oz_run_id and session_link via callback
            if self._callbacks.on_run_created:
                try:
                    session_link = getattr(response, "session_link", None)
                    await self._callbacks.on_run_created(execution_id, response.run_id, session_link)
                except Exception as e:
                    logger.warning(f"on_run_created callback failed: {e}")

            await event_bus.publish(
                EventType.AGENT_STARTED,
                {
                    "execution_id": str(execution_id),
                    "repo_url": config.repo_url,
                    "oz_run_id": response.run_id,
                },
            )
            logger.info(f"Created Oz run {response.run_id} for execution {execution_id}")

        except Exception as e:
            logger.error(f"Failed to create Oz run: {e}")
            execution.status = ExecutionStatus.FAILED
            execution.result = f"Failed to create Oz run: {e}"
            execution.completed_at = utc_now()
            self._executions.pop(execution_id, None)
            raise

        return execution_id

    async def start_polling(self) -> None:
        """Start the background polling loop for run completion.

        Also recovers in-flight Oz runs via callback so that runs
        launched before a process restart are still tracked.
        """
        if self._polling:
            return

        # Recover in-flight runs via callback
        if self._callbacks.recover_runs:
            try:
                active_runs = await self._callbacks.recover_runs()
                for exec_id, run_id in active_runs:
                    if exec_id not in self._run_map:
                        self._run_map[exec_id] = run_id
                if active_runs:
                    logger.info(f"Recovered {len(active_runs)} in-flight Oz runs")
            except Exception as e:
                logger.warning(f"Failed to recover Oz runs: {e}")

        self._polling = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(f"Oz polling started (interval={settings.oz_poll_interval_seconds}s)")

    async def stop_polling(self) -> None:
        """Stop the background polling loop."""
        self._polling = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def close(self) -> None:
        """Stop polling and close the underlying HTTP client."""
        await self.stop_polling()
        await self._client.close()

    async def _poll_loop(self) -> None:
        """Background loop that checks Oz for completed runs."""
        while self._polling:
            try:
                await self._poll_runs()
            except Exception:
                logger.exception("Error polling Oz runs")
            await asyncio.sleep(settings.oz_poll_interval_seconds)

    async def _poll_runs(self) -> None:
        """Check all tracked runs for completion."""
        if not self._run_map:
            return

        for exec_id, run_id in list(self._run_map.items()):
            try:
                run = await self._client.agent.runs.retrieve(run_id)

                # Guard: execution may have been cancelled during the await above
                if exec_id not in self._run_map:
                    continue

                if run.state not in _TERMINAL_STATES:
                    continue

                # Extract PR artifact if present
                artifacts = RunArtifacts()
                if run.artifacts:
                    for artifact in run.artifacts:
                        if artifact.artifact_type == "PULL_REQUEST":
                            artifacts.branch = artifact.data.branch
                            artifacts.pr_url = artifact.data.url
                            pr_match = re.search(r"/pull/(\d+)", artifacts.pr_url or "")
                            if pr_match:
                                artifacts.pr_number = int(pr_match.group(1))
                            break

                artifacts.result = run.status_message.message if run.status_message else None

                # Capture cost from request_usage
                request_usage = getattr(run, "request_usage", None)
                if request_usage:
                    compute = getattr(request_usage, "compute_cost", 0) or 0
                    inference = getattr(request_usage, "inference_cost", 0) or 0
                    total = compute + inference
                    if total > 0:
                        artifacts.cost_cents = int(total * 100)

                if run.state == "SUCCEEDED":
                    # Let the callback handle DB updates, fallback PR detection, cost
                    if self._callbacks.on_run_succeeded:
                        try:
                            artifacts = await self._callbacks.on_run_succeeded(exec_id, artifacts)
                        except Exception as e:
                            logger.warning(f"on_run_succeeded callback failed for {exec_id}: {e}")

                    execution = self._executions.get(exec_id)
                    if execution:
                        execution.status = ExecutionStatus.COMPLETED
                        execution.completed_at = utc_now()
                        execution.result = artifacts.result

                    await event_bus.publish(
                        EventType.AGENT_COMPLETED,
                        {
                            "execution_id": str(exec_id),
                            "result": artifacts.result,
                            "branch": artifacts.branch,
                            "pr_number": artifacts.pr_number,
                            "pr_url": artifacts.pr_url,
                            "oz_run_id": run_id,
                        },
                    )
                    logger.info(f"Oz run {run_id} succeeded (execution {exec_id})")

                else:
                    # FAILED or CANCELLED
                    error_msg = artifacts.result or f"Run {run.state.lower()}"

                    if self._callbacks.on_run_failed:
                        try:
                            await self._callbacks.on_run_failed(exec_id, error_msg)
                        except Exception as e:
                            logger.warning(f"on_run_failed callback failed for {exec_id}: {e}")

                    execution = self._executions.get(exec_id)
                    if execution:
                        execution.status = ExecutionStatus.FAILED
                        execution.completed_at = utc_now()
                        execution.result = error_msg

                    await event_bus.publish(
                        EventType.AGENT_FAILED,
                        {
                            "execution_id": str(exec_id),
                            "error": error_msg,
                            "oz_run_id": run_id,
                        },
                    )
                    logger.info(f"Oz run {run_id} {run.state.lower()} (execution {exec_id})")

                # Clean up tracking
                self._executions.pop(exec_id, None)
                self._run_map.pop(exec_id, None)
                self._poll_errors.pop(exec_id, None)

            except Exception:
                logger.exception(f"Error checking Oz run {run_id}")
                self._poll_errors[exec_id] = self._poll_errors.get(exec_id, 0) + 1
                if self._poll_errors[exec_id] >= 10:
                    logger.error(f"Giving up on Oz run {run_id} after 10 poll failures")
                    self._run_map.pop(exec_id, None)
                    self._executions.pop(exec_id, None)
                    self._poll_errors.pop(exec_id, None)
                    await event_bus.publish(
                        EventType.AGENT_FAILED,
                        {"execution_id": str(exec_id), "error": "Lost contact with Oz run"},
                    )

    async def get_execution_status(self, execution_id: UUID) -> AgentExecution | None:
        return self._executions.get(execution_id)

    def get_active_executions(self) -> list[AgentExecution]:
        return list(self._executions.values())

    async def cancel_execution(self, execution_id: UUID) -> bool:
        run_id = self._run_map.get(execution_id)
        if run_id:
            try:
                await self._client.agent.runs.cancel(run_id)
            except Exception as e:
                logger.warning(f"Failed to cancel Oz run {run_id}: {e}")

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
            self._run_map.pop(execution_id, None)
            return True
        return False

    def subscribe_to_agent_events(self, handler: AgentEventHandler) -> None:
        async def event_handler(event: Event) -> None:
            await handler(event.type.value, event.payload)

        self._handler_mapping[handler] = event_handler
        event_bus.subscribe(event_handler, event_type=None)

    def unsubscribe_from_agent_events(self, handler: AgentEventHandler) -> None:
        event_handler = self._handler_mapping.pop(handler, None)
        if event_handler:
            event_bus.unsubscribe(event_handler, event_type=None)


_oz_grid: OzExecutionGrid | None = None


def get_oz_execution_grid() -> OzExecutionGrid:
    global _oz_grid
    if _oz_grid is None:
        _oz_grid = OzExecutionGrid()
    return _oz_grid
