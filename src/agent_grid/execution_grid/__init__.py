"""Execution grid module for launching and managing coding agents."""

from .claude_code_grid import ClaudeCodeExecutionGrid, get_claude_code_execution_grid
from .event_bus import EventBus, event_bus
from .fly_grid import FlyExecutionGrid, get_fly_execution_grid
from .oz_grid import OzExecutionGrid, get_oz_execution_grid
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
    # Claude Code CLI execution grid (coordinator mode)
    "ClaudeCodeExecutionGrid",
    "get_claude_code_execution_grid",
    # Fly Machines-based execution grid (coordinator mode)
    "FlyExecutionGrid",
    "get_fly_execution_grid",
    # Warp Oz-based execution grid (coordinator mode)
    "OzExecutionGrid",
    "get_oz_execution_grid",
]
