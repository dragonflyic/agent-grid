"""Real-time agent event logger.

Subscribes to all agent events and streams them to server logs.
"""

import logging

from ..execution_grid import get_execution_grid

logger = logging.getLogger("agent_grid.agent")


class AgentEventLogger:
    """
    Logs all agent events to the server logs in real-time.

    Uses the execution_grid's public subscription API.
    """

    def __init__(self):
        self._running = False
        self._grid = get_execution_grid()

    async def start(self) -> None:
        """Start the logger and subscribe to agent events."""
        self._running = True
        self._grid.subscribe_to_agent_events(self._handle_event)
        logger.info("Agent event logger started - streaming all agent events to logs")

    async def stop(self) -> None:
        """Stop the logger."""
        self._running = False
        self._grid.unsubscribe_from_agent_events(self._handle_event)

    async def _handle_event(self, event_type: str, payload: dict) -> None:
        """Handle incoming agent event."""
        if not self._running:
            return

        execution_id = payload.get("execution_id", "unknown")[:8]

        if event_type == "agent.started":
            self._log_started(execution_id, payload)
        elif event_type == "agent.progress":
            self._log_progress(execution_id, payload)
        elif event_type == "agent.chat":
            self._log_chat(execution_id, payload)
        elif event_type == "agent.completed":
            self._log_completed(execution_id, payload)
        elif event_type == "agent.failed":
            self._log_failed(execution_id, payload)

    def _log_started(self, exec_id: str, payload: dict) -> None:
        """Log agent started event."""
        issue_id = payload.get("issue_id", "?")
        repo_url = payload.get("repo_url", "?")
        # Extract repo name from URL
        repo = repo_url.replace("https://github.com/", "").replace(".git", "")
        logger.info(f"[{exec_id}] 🚀 STARTED - issue={issue_id} repo={repo}")

    def _log_progress(self, exec_id: str, payload: dict) -> None:
        """Log agent progress event."""
        # Progress events are lower-level than chat, skip to reduce noise
        # Chat events provide more detail
        pass

    def _log_chat(self, exec_id: str, payload: dict) -> None:
        """Log agent chat message."""
        message_type = payload.get("message_type", "unknown")
        content = payload.get("content", "")
        tool_name = payload.get("tool_name")
        tool_id = payload.get("tool_id")

        if message_type == "text":
            self._log_text(exec_id, content)
        elif message_type == "tool_use":
            self._log_tool_use(exec_id, tool_name, tool_id, content)
        elif message_type == "tool_result":
            self._log_tool_result(exec_id, tool_id, content)
        elif message_type == "system":
            self._log_system(exec_id, content)
        elif message_type == "result":
            self._log_result(exec_id, content)

    def _log_completed(self, exec_id: str, payload: dict) -> None:
        """Log agent completed event."""
        result = payload.get("result", "")
        preview = result[:100].replace("\n", " ") if result else "(no result)"
        if len(result) > 100:
            preview += "..."
        logger.info(f"[{exec_id}] ✅ COMPLETED - {preview}")

    def _log_failed(self, exec_id: str, payload: dict) -> None:
        """Log agent failed event."""
        error = payload.get("error", "unknown error")
        logger.error(f"[{exec_id}] ❌ FAILED - {error}")

    def _log_text(self, exec_id: str, content: str) -> None:
        """Log assistant text message."""
        lines = content.split("\n")
        if len(lines) > 1:
            logger.info(f"[{exec_id}] 💬 Assistant:")
            for line in lines:
                if line.strip():
                    logger.info(f"[{exec_id}]    {line}")
        else:
            logger.info(f"[{exec_id}] 💬 {content}")

    def _log_tool_use(self, exec_id: str, tool_name: str | None, tool_id: str | None, content: str) -> None:
        """Log tool use."""
        short_tool_id = tool_id[:8] if tool_id else "?"
        logger.info(f"[{exec_id}] 🔧 Tool: {tool_name} (id: {short_tool_id})")
        if content and len(content) < 500:
            for line in content.split("\n"):
                logger.info(f"[{exec_id}]    {line}")
        elif content:
            preview = content[:200].replace("\n", " ")
            logger.info(f"[{exec_id}]    {preview}...")

    def _log_tool_result(self, exec_id: str, tool_id: str | None, content: str) -> None:
        """Log tool result."""
        short_tool_id = tool_id[:8] if tool_id else "?"
        preview = content[:150].replace("\n", " ") if content else "(empty)"
        if len(content) > 150:
            preview += "..."
        logger.info(f"[{exec_id}] 📎 Result (id: {short_tool_id}): {preview}")

    def _log_system(self, exec_id: str, content: str) -> None:
        """Log system message."""
        logger.info(f"[{exec_id}] ⚙️  System: {content}")

    def _log_result(self, exec_id: str, content: str) -> None:
        """Log final result."""
        logger.info(f"[{exec_id}] 🏁 Final result:")
        for line in content.split("\n")[:10]:
            if line.strip():
                logger.info(f"[{exec_id}]    {line}")
        if content.count("\n") > 10:
            logger.info(f"[{exec_id}]    ... (truncated)")


# Global instance
_agent_event_logger: AgentEventLogger | None = None


def get_agent_event_logger() -> AgentEventLogger:
    """Get the global agent event logger instance."""
    global _agent_event_logger
    if _agent_event_logger is None:
        _agent_event_logger = AgentEventLogger()
    return _agent_event_logger
