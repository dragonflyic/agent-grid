"""Claude Code CLI execution grid.

Spawns Fly Machines running Claude Code CLI with full skills/CLAUDE.md support.
Workers stream events to the coordinator and POST callbacks on completion.
Sessions are persisted to S3 for resume across machines.
"""

import logging
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

logger = logging.getLogger("agent_grid.claude_code_grid")


@dataclass
class RunArtifacts:
    """Artifacts produced by a Claude Code CLI execution."""

    branch: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    result: str | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    session_s3_key: str | None = None


@dataclass
class ClaudeCodeCallbacks:
    """Injectable callbacks for cross-layer operations.

    Set via ``ClaudeCodeExecutionGrid.set_callbacks()`` during startup.
    All callbacks are optional -- if not set, the operation is skipped.
    """

    # Called when execution completes: (execution_id, artifacts) -> updated artifacts
    on_execution_completed: Callable[
        [UUID, RunArtifacts], Awaitable[RunArtifacts]
    ] | None = None

    # Called when execution fails: (execution_id, error_message)
    on_execution_failed: Callable[[UUID, str], Awaitable[None]] | None = None


class ClaudeCodeExecutionGrid(ExecutionGrid):
    """Spawns Fly Machines running Claude Code CLI.

    - Stores the prompt in S3 (too large for env var)
    - Passes session config (S3 keys, coordinator URL) as env vars
    - Workers POST results back to /api/agent-status
    - Tracks executions in-memory; cleaned up after callback
    """

    def __init__(self) -> None:
        self._executions: dict[UUID, AgentExecution] = {}
        self._machine_map: dict[UUID, str] = {}  # execution_id -> fly_machine_id
        self._callbacks = ClaudeCodeCallbacks()
        self._handler_mapping: dict[
            AgentEventHandler, Callable[[Event], Awaitable[None]]
        ] = {}

    def set_callbacks(self, callbacks: ClaudeCodeCallbacks) -> None:
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
        """Launch an agent on an ephemeral Fly Machine running Claude Code CLI."""
        execution_id = execution_id or uuid4()
        execution = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=config.prompt,
            mode=mode,
            started_at=utc_now(),
        )
        self._executions[execution_id] = execution
        context = context or {}

        # Store prompt in S3 (too large for env var)
        prompt_s3_key = f"prompts/{execution_id}.txt"
        await self._upload_to_s3(prompt_s3_key, config.prompt)

        # Get fresh GitHub installation token
        from ..github_app import get_github_app_auth

        try:
            app_auth = get_github_app_auth()
            github_token = await app_auth.get_installation_token()
        except Exception as e:
            logger.warning(f"Failed to get GitHub token: {e}")
            github_token = ""

        # Build environment variables for the Fly Machine
        env: dict[str, str] = {
            "EXECUTION_ID": str(execution_id),
            "REPO_URL": config.repo_url,
            "ISSUE_NUMBER": str(issue_number or ""),
            "MODE": mode,
            "PROMPT_S3_KEY": prompt_s3_key,
            "COORDINATOR_URL": (
                settings.coordinator_url
                or f"https://{settings.fly_app_name}.fly.dev"
            ),
            "GITHUB_TOKEN": github_token,
            "CLAUDE_CREDENTIALS_SECRET": settings.claude_credentials_secret,
            "S3_SESSION_BUCKET": settings.session_s3_bucket,
            "MAX_TURNS": str(settings.max_turns_per_execution),
            "MAX_BUDGET_USD": str(settings.max_budget_per_execution_usd),
            "AWS_REGION": settings.aws_region,
        }

        # Add fallback API key
        if settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

        # Session resume support
        resume_session_id = context.get("resume_session_id")
        if resume_session_id:
            env["RESUME_SESSION_ID"] = str(resume_session_id)

        # Spawn Fly Machine
        from ..fly.machines import get_fly_client

        fly = get_fly_client()

        try:
            machine = await fly.spawn_worker_v2(
                execution_id=str(execution_id),
                env=env,
                issue_number=issue_number or 0,
            )
            machine_id = machine.get("id", "")
            self._machine_map[execution_id] = machine_id
            logger.info(
                f"Launched Fly Machine {machine_id} for execution {execution_id}"
            )
        except Exception as e:
            logger.error(
                f"Failed to launch Fly Machine for {execution_id}: {e}"
            )
            execution.status = ExecutionStatus.FAILED
            execution.result = f"Launch failed: {e}"
            execution.completed_at = utc_now()
            if self._callbacks.on_execution_failed:
                await self._callbacks.on_execution_failed(execution_id, str(e))
            await event_bus.publish(
                EventType.AGENT_FAILED,
                {"execution_id": str(execution_id), "error": str(e)},
            )
            return execution_id

        await event_bus.publish(
            EventType.AGENT_STARTED,
            {
                "execution_id": str(execution_id),
                "repo_url": config.repo_url,
                "machine_id": machine_id,
            },
        )
        return execution_id

    async def handle_agent_result(
        self,
        execution_id: UUID,
        status: str,
        result: str | None = None,
        branch: str | None = None,
        pr_number: int | None = None,
        cost_usd: float | None = None,
        session_id: str | None = None,
        session_s3_key: str | None = None,
    ) -> None:
        """Handle callback from worker when execution completes.

        Called by the ``/api/agent-status`` endpoint.
        """
        execution = self._executions.get(execution_id)

        artifacts = RunArtifacts(
            branch=branch,
            pr_number=pr_number,
            result=result,
            cost_usd=cost_usd,
            session_id=session_id,
            session_s3_key=session_s3_key,
        )

        if status == "completed":
            # Run completion callback (PR detection, DB updates)
            if self._callbacks.on_execution_completed:
                try:
                    artifacts = await self._callbacks.on_execution_completed(
                        execution_id, artifacts
                    )
                except Exception as e:
                    logger.warning(
                        f"Completion callback failed for {execution_id}: {e}"
                    )

            if execution:
                execution.status = ExecutionStatus.COMPLETED
                execution.result = artifacts.result
                execution.completed_at = utc_now()

            await event_bus.publish(
                EventType.AGENT_COMPLETED,
                {
                    "execution_id": str(execution_id),
                    "result": artifacts.result,
                    "branch": artifacts.branch,
                    "pr_number": artifacts.pr_number,
                },
            )
            logger.info(f"Execution {execution_id} completed")
        else:
            # Failed
            error_msg = result or "Execution failed"
            if self._callbacks.on_execution_failed:
                try:
                    await self._callbacks.on_execution_failed(
                        execution_id, error_msg
                    )
                except Exception as e:
                    logger.warning(
                        f"Failure callback failed for {execution_id}: {e}"
                    )

            if execution:
                execution.status = ExecutionStatus.FAILED
                execution.result = error_msg
                execution.completed_at = utc_now()

            await event_bus.publish(
                EventType.AGENT_FAILED,
                {"execution_id": str(execution_id), "error": error_msg},
            )
            logger.info(f"Execution {execution_id} failed")

        # Clean up tracking
        self._executions.pop(execution_id, None)
        self._machine_map.pop(execution_id, None)

    async def get_execution_status(
        self, execution_id: UUID
    ) -> AgentExecution | None:
        return self._executions.get(execution_id)

    def get_active_executions(self) -> list[AgentExecution]:
        return [
            e
            for e in self._executions.values()
            if e.status in (ExecutionStatus.PENDING, ExecutionStatus.RUNNING)
        ]

    async def cancel_execution(self, execution_id: UUID) -> bool:
        machine_id = self._machine_map.get(execution_id)
        if machine_id:
            try:
                from ..fly.machines import get_fly_client

                fly = get_fly_client()
                await fly.destroy_machine(machine_id)
            except Exception as e:
                logger.warning(f"Failed to destroy machine {machine_id}: {e}")

        execution = self._executions.get(execution_id)
        if execution:
            execution.status = ExecutionStatus.FAILED
            execution.result = "Cancelled"
            execution.completed_at = utc_now()
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

        self._handler_mapping[handler] = event_handler
        event_bus.subscribe(event_handler, event_type=None)

    def unsubscribe_from_agent_events(self, handler: AgentEventHandler) -> None:
        event_handler = self._handler_mapping.pop(handler, None)
        if event_handler:
            event_bus.unsubscribe(event_handler, event_type=None)

    async def close(self) -> None:
        """Cancel all running executions."""
        for execution_id in list(self._machine_map.keys()):
            await self.cancel_execution(execution_id)

    # --- Helpers ---

    async def _upload_to_s3(self, key: str, content: str) -> None:
        """Upload content to S3 session bucket."""
        import boto3

        try:
            bucket = settings.session_s3_bucket
            if not bucket:
                logger.warning(
                    "No S3 session bucket configured, skipping prompt upload"
                )
                return
            s3 = boto3.client("s3", region_name=settings.aws_region)
            s3.put_object(Bucket=bucket, Key=key, Body=content.encode())
        except Exception as e:
            logger.warning(f"Failed to upload to S3: {e}")


_grid: ClaudeCodeExecutionGrid | None = None


def get_claude_code_execution_grid() -> ClaudeCodeExecutionGrid:
    """Get the global ClaudeCodeExecutionGrid singleton."""
    global _grid
    if _grid is None:
        _grid = ClaudeCodeExecutionGrid()
    return _grid
