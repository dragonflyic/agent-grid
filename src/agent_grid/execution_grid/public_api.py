"""Public API for execution grid module.

This module defines the public interface and models for the execution grid.
Implementation modules import from here, not the other way around.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Awaitable
from uuid import UUID

from pydantic import BaseModel, Field


# =============================================================================
# Utilities
# =============================================================================

def utc_now() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


# =============================================================================
# Models
# =============================================================================

class ExecutionConfig(BaseModel):
    """Configuration for a generic agent execution."""

    repo_url: str  # URL of repository to clone
    prompt: str  # Full instructions for the agent
    permission_mode: str = "bypassPermissions"  # ClaudeCodeOptions.permission_mode


class ExecutionStatus(str, Enum):
    """Status of an agent execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EventType(str, Enum):
    """Types of events in the event bus."""

    AGENT_STARTED = "agent.started"
    AGENT_PROGRESS = "agent.progress"
    AGENT_CHAT = "agent.chat"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    ISSUE_CREATED = "issue.created"
    ISSUE_UPDATED = "issue.updated"
    NUDGE_REQUESTED = "nudge.requested"
    PR_REVIEW = "pr.review"
    PR_CLOSED = "pr.closed"


class Event(BaseModel):
    """An event in the event bus."""

    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentExecution(BaseModel):
    """Represents an agent execution."""

    id: UUID
    repo_url: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    prompt: str | None = None
    result: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


# =============================================================================
# Type Aliases
# =============================================================================

AgentEventHandler = Callable[[str, dict], Awaitable[None]]
"""Handler for agent events. Called with (event_type: str, payload: dict)."""


# =============================================================================
# Service Interface (ABC)
# =============================================================================

class ExecutionGrid(ABC):
    """Abstract interface for the execution grid service."""

    @abstractmethod
    async def launch_agent(self, config: ExecutionConfig) -> UUID:
        """
        Launch a generic Claude Code session.

        Args:
            config: Configuration for the execution (repo_url, prompt, permission_mode).

        Returns:
            Execution ID for tracking.
        """
        pass

    @abstractmethod
    async def get_execution_status(self, execution_id: UUID) -> AgentExecution | None:
        """
        Get the status of an execution.

        Args:
            execution_id: The execution ID.

        Returns:
            AgentExecution if found, None otherwise.
        """
        pass

    @abstractmethod
    def get_active_executions(self) -> list[AgentExecution]:
        """
        Get all active executions.

        Returns:
            List of active AgentExecution objects.
        """
        pass

    @abstractmethod
    async def cancel_execution(self, execution_id: UUID) -> bool:
        """
        Cancel an active execution.

        Args:
            execution_id: The execution ID to cancel.

        Returns:
            True if cancelled, False if not found.
        """
        pass

    @abstractmethod
    def subscribe_to_agent_events(self, handler: AgentEventHandler) -> None:
        """
        Subscribe to all agent execution events.

        The handler will be called with (event_type, payload) where:
        - event_type: str - One of 'agent.started', 'agent.progress', 'agent.chat',
                            'agent.completed', 'agent.failed'
        - payload: dict - Event-specific data (always includes 'execution_id')

        Args:
            handler: Async function to call for each event.
        """
        pass

    @abstractmethod
    def unsubscribe_from_agent_events(self, handler: AgentEventHandler) -> None:
        """
        Unsubscribe from agent events.

        Args:
            handler: The handler that was previously subscribed.
        """
        pass
