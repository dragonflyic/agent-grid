"""Handler for nudge requests from agents."""

from uuid import UUID, uuid4

from ..execution_grid import EventType, event_bus
from .database import get_database
from .public_api import NudgeRequest


class NudgeHandler:
    """
    Handles nudge requests from agents.

    Nudges are requests from one agent to start work on another issue.
    """

    def __init__(self):
        self._db = get_database()

    async def handle_nudge(
        self,
        issue_id: str,
        repo: str | None = None,
        source_execution_id: UUID | None = None,
        priority: int = 0,
        reason: str | None = None,
    ) -> NudgeRequest:
        """
        Handle a nudge request.

        Args:
            issue_id: The issue to nudge.
            repo: Repository in owner/name format.
            source_execution_id: Optional ID of the execution that requested the nudge.
            priority: Priority level (higher = more urgent).
            reason: Optional reason for the nudge.

        Returns:
            The created NudgeRequest.
        """
        nudge = NudgeRequest(
            id=uuid4(),
            issue_id=issue_id,
            source_execution_id=source_execution_id,
            priority=priority,
            reason=reason,
        )

        # Store in database
        await self._db.create_nudge(nudge)

        # Publish event
        await event_bus.publish(
            EventType.NUDGE_REQUESTED,
            {
                "nudge_id": str(nudge.id),
                "issue_id": issue_id,
                "repo": repo,
                "source_execution_id": str(source_execution_id) if source_execution_id else None,
                "priority": priority,
                "reason": reason,
            },
        )

        return nudge

    async def get_pending_nudges(self, limit: int = 10) -> list[NudgeRequest]:
        """Get pending nudge requests."""
        return await self._db.get_pending_nudges(limit)

    async def mark_processed(self, nudge_id: UUID) -> None:
        """Mark a nudge as processed."""
        await self._db.mark_nudge_processed(nudge_id)


# Global instance
_nudge_handler: NudgeHandler | None = None


def get_nudge_handler() -> NudgeHandler:
    """Get the global nudge handler instance."""
    global _nudge_handler
    if _nudge_handler is None:
        _nudge_handler = NudgeHandler()
    return _nudge_handler
