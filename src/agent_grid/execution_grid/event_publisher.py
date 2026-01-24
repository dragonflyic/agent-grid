"""Event publishing for execution status updates."""

from uuid import UUID

from ..common import event_bus, EventType


class ExecutionEventPublisher:
    """Publishes execution-related events to the event bus."""

    async def agent_started(
        self,
        execution_id: UUID,
        issue_id: str,
        repo_url: str,
    ) -> None:
        """Publish agent started event."""
        await event_bus.publish(
            EventType.AGENT_STARTED,
            {
                "execution_id": str(execution_id),
                "issue_id": issue_id,
                "repo_url": repo_url,
            },
        )

    async def agent_progress(
        self,
        execution_id: UUID,
        message: str,
        progress_type: str = "info",
    ) -> None:
        """Publish agent progress event."""
        await event_bus.publish(
            EventType.AGENT_PROGRESS,
            {
                "execution_id": str(execution_id),
                "message": message,
                "type": progress_type,
            },
        )

    async def agent_completed(
        self,
        execution_id: UUID,
        result: str | None = None,
    ) -> None:
        """Publish agent completed event."""
        await event_bus.publish(
            EventType.AGENT_COMPLETED,
            {
                "execution_id": str(execution_id),
                "result": result,
            },
        )

    async def agent_failed(
        self,
        execution_id: UUID,
        error: str,
    ) -> None:
        """Publish agent failed event."""
        await event_bus.publish(
            EventType.AGENT_FAILED,
            {
                "execution_id": str(execution_id),
                "error": error,
            },
        )


# Global instance
event_publisher = ExecutionEventPublisher()
