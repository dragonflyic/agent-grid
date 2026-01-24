"""Public API for execution grid module."""

from uuid import UUID, uuid4

from ..common.models import AgentExecution, ExecutionStatus
from .agent_runner import get_agent_runner


async def launch_agent(
    issue_id: str,
    repo_url: str,
    prompt: str,
) -> UUID:
    """
    Launch a coding agent for an issue.

    Args:
        issue_id: The issue ID this agent is working on.
        repo_url: URL of the repository to clone.
        prompt: Instructions for the agent.

    Returns:
        Execution ID for tracking.
    """
    execution_id = uuid4()

    execution = AgentExecution(
        id=execution_id,
        issue_id=issue_id,
        repo_url=repo_url,
        status=ExecutionStatus.PENDING,
        prompt=prompt,
    )

    runner = get_agent_runner()
    runner.start_execution(execution, prompt)

    return execution_id


async def get_execution_status(execution_id: UUID) -> AgentExecution | None:
    """
    Get the status of an execution.

    Args:
        execution_id: The execution ID.

    Returns:
        AgentExecution if found, None otherwise.
    """
    runner = get_agent_runner()
    return runner.get_execution(execution_id)


def get_active_executions() -> list[AgentExecution]:
    """
    Get all active executions.

    Returns:
        List of active AgentExecution objects.
    """
    runner = get_agent_runner()
    return runner.get_active_executions()


async def cancel_execution(execution_id: UUID) -> bool:
    """
    Cancel an active execution.

    Args:
        execution_id: The execution ID to cancel.

    Returns:
        True if cancelled, False if not found.
    """
    runner = get_agent_runner()
    return await runner.cancel_execution(execution_id)
