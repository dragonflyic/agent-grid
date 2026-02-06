"""Event publishing for execution status updates."""

from uuid import UUID

from .event_bus import event_bus
from .public_api import EventType


class ExecutionEventPublisher:
    """Publishes execution-related events to the event bus."""

    async def agent_started(
        self,
        execution_id: UUID,
        repo_url: str,
    ) -> None:
        """Publish agent started event."""
        await event_bus.publish(
            EventType.AGENT_STARTED,
            {
                "execution_id": str(execution_id),
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

    async def agent_chat(
        self,
        execution_id: UUID,
        message_type: str,
        content: str,
        tool_name: str | None = None,
        tool_id: str | None = None,
    ) -> None:
        """
        Publish detailed agent chat message for real-time streaming.

        Args:
            execution_id: The execution ID.
            message_type: One of 'text', 'tool_use', 'tool_result', 'system', 'result'.
            content: The message content.
            tool_name: Tool name if message_type is 'tool_use' or 'tool_result'.
            tool_id: Tool ID for correlating tool use with results.
        """
        await event_bus.publish(
            EventType.AGENT_CHAT,
            {
                "execution_id": str(execution_id),
                "message_type": message_type,
                "content": content,
                "tool_name": tool_name,
                "tool_id": tool_id,
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
