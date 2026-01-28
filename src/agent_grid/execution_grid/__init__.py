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
from .sqs_client import SQSClient, get_sqs_client, JobRequest, JobResult
from .sqs_grid import ExecutionGridClient, get_sqs_execution_grid

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
    # SQS client (for hybrid deployment)
    "SQSClient",
    "get_sqs_client",
    "JobRequest",
    "JobResult",
    # SQS-based execution grid (coordinator mode)
    "ExecutionGridClient",
    "get_sqs_execution_grid",
]
