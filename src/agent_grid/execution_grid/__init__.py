"""Execution grid module for launching and managing coding agents."""

from .event_bus import EventBus, event_bus
from .fly_grid import FlyExecutionGrid, get_fly_execution_grid
from .public_api import (
    # Type aliases
    AgentEventHandler,
    # Models
    AgentExecution,
    Event,
    EventType,
    ExecutionConfig,
    # ABC interface
    ExecutionGrid,
    ExecutionStatus,
    utc_now,
)
from .service import ExecutionGridService, get_execution_grid

__all__ = [
    # Public API - Models
    "AgentExecution",
    "Event",
    "EventType",
    "ExecutionConfig",
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
    # Fly Machines-based execution grid (coordinator mode)
    "FlyExecutionGrid",
    "get_fly_execution_grid",
]
