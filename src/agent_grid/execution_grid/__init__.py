"""Execution grid module for launching and managing coding agents."""

from .public_api import (
    # Models
    AgentExecution,
    Event,
    EventType,
    ExecutionStatus,
    utc_now,
    # Type aliases
    AgentEventHandler,
    # ABC interface
    ExecutionGrid,
)
from .service import get_execution_grid, ExecutionGridService
from .event_bus import EventBus, event_bus

__all__ = [
    # Public API - Models
    "AgentExecution",
    "Event",
    "EventType",
    "ExecutionStatus",
    "utc_now",
    # Public API - Type aliases
    "AgentEventHandler",
    # Public API - Interface and service
    "ExecutionGrid",
    "ExecutionGridService",
    "get_execution_grid",
    # Event bus (for startup/shutdown)
    "EventBus",
    "event_bus",
]
