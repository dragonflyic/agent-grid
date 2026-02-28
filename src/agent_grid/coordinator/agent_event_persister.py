"""Persists agent events to the database for dashboard observability.

Subscribes to AGENT_CHAT events on the event bus and writes them
to the agent_events table. Runs alongside AgentEventLogger (chat_logger.py).
"""

import logging
from uuid import UUID

from ..execution_grid import get_execution_grid
from .database import get_database

logger = logging.getLogger("agent_grid.event_persister")


class AgentEventPersister:
    """Persists agent chat events to the database."""

    def __init__(self):
        self._running = False
        self._grid = get_execution_grid()

    async def start(self) -> None:
        self._running = True
        self._grid.subscribe_to_agent_events(self._handle_event)
        logger.info("Agent event persister started")

    async def stop(self) -> None:
        self._running = False
        self._grid.unsubscribe_from_agent_events(self._handle_event)

    async def _handle_event(self, event_type: str, payload: dict) -> None:
        if not self._running:
            return

        # Only persist chat events (text, tool_use, tool_result, system, result)
        if event_type != "agent.chat":
            return

        execution_id_str = payload.get("execution_id")
        if not execution_id_str:
            return

        try:
            db = get_database()

            # Truncate very large content to prevent DB bloat
            content = payload.get("content")
            if content and len(content) > 10_000:
                content = content[:10_000] + "\n... [truncated]"

            await db.record_agent_event(
                execution_id=UUID(execution_id_str),
                message_type=payload.get("message_type", "unknown"),
                content=content,
                tool_name=payload.get("tool_name"),
                tool_id=payload.get("tool_id"),
            )
        except Exception:
            logger.exception(f"Failed to persist agent event for {execution_id_str[:8]}")


# Global instance
_agent_event_persister: AgentEventPersister | None = None


def get_agent_event_persister() -> AgentEventPersister:
    global _agent_event_persister
    if _agent_event_persister is None:
        _agent_event_persister = AgentEventPersister()
    return _agent_event_persister
