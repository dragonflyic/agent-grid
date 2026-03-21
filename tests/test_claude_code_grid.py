"""Tests for Claude Code CLI execution grid."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from agent_grid.execution_grid.claude_code_grid import (
    ClaudeCodeCallbacks,
    ClaudeCodeExecutionGrid,
    RunArtifacts,
)
from agent_grid.execution_grid.public_api import (
    AgentExecution,
    ExecutionStatus,
)


class TestClaudeCodeGrid:
    def _make_grid(self):
        grid = ClaudeCodeExecutionGrid()
        return grid

    def test_initial_state(self):
        grid = self._make_grid()
        assert grid.get_active_executions() == []

    @pytest.mark.asyncio
    async def test_handle_agent_result_completed(self):
        grid = self._make_grid()
        exec_id = uuid4()

        # Pre-populate execution
        grid._executions[exec_id] = AgentExecution(
            id=exec_id,
            repo_url="https://github.com/test/repo.git",
            status=ExecutionStatus.RUNNING,
            prompt="test",
        )

        completed_callback = AsyncMock(
            return_value=RunArtifacts(
                branch="agent/42",
                pr_number=100,
                result="Done",
                cost_usd=1.5,
            )
        )
        grid.set_callbacks(
            ClaudeCodeCallbacks(
                on_execution_completed=completed_callback,
            )
        )

        await grid.handle_agent_result(
            execution_id=exec_id,
            status="completed",
            result="Done",
            branch="agent/42",
            pr_number=100,
            cost_usd=1.5,
        )

        completed_callback.assert_called_once()
        # Execution should be cleaned up
        assert exec_id not in grid._executions

    @pytest.mark.asyncio
    async def test_handle_agent_result_failed(self):
        grid = self._make_grid()
        exec_id = uuid4()

        grid._executions[exec_id] = AgentExecution(
            id=exec_id,
            repo_url="https://github.com/test/repo.git",
            status=ExecutionStatus.RUNNING,
            prompt="test",
        )

        failed_callback = AsyncMock()
        grid.set_callbacks(
            ClaudeCodeCallbacks(
                on_execution_failed=failed_callback,
            )
        )

        await grid.handle_agent_result(
            execution_id=exec_id,
            status="failed",
            result="Error: something broke",
        )

        failed_callback.assert_called_once()
        assert exec_id not in grid._executions

    @pytest.mark.asyncio
    async def test_handle_agent_result_unknown_execution(self):
        """handle_agent_result should not crash for an unknown execution_id."""
        grid = self._make_grid()
        exec_id = uuid4()

        completed_callback = AsyncMock(return_value=RunArtifacts(result="Done"))
        grid.set_callbacks(ClaudeCodeCallbacks(on_execution_completed=completed_callback))

        # Should not raise even though exec_id is not tracked
        await grid.handle_agent_result(
            execution_id=exec_id,
            status="completed",
            result="Done",
        )

        completed_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_execution(self):
        grid = self._make_grid()
        exec_id = uuid4()

        grid._executions[exec_id] = AgentExecution(
            id=exec_id,
            repo_url="https://github.com/test/repo.git",
            status=ExecutionStatus.RUNNING,
            prompt="test",
        )
        grid._machine_map[exec_id] = "fly-machine-123"

        with patch("agent_grid.fly.machines.get_fly_client") as mock_fly:
            mock_client = MagicMock()
            mock_client.destroy_machine = AsyncMock()
            mock_fly.return_value = mock_client

            result = await grid.cancel_execution(exec_id)
            assert result is True
            assert exec_id not in grid._executions

    @pytest.mark.asyncio
    async def test_cancel_execution_not_found(self):
        grid = self._make_grid()
        result = await grid.cancel_execution(uuid4())
        assert result is False

    @pytest.mark.asyncio
    async def test_get_execution_status(self):
        grid = self._make_grid()
        exec_id = uuid4()

        # Not found
        assert await grid.get_execution_status(exec_id) is None

        # Found
        execution = AgentExecution(
            id=exec_id,
            repo_url="https://github.com/test/repo.git",
            status=ExecutionStatus.RUNNING,
            prompt="test",
        )
        grid._executions[exec_id] = execution
        assert await grid.get_execution_status(exec_id) is execution

    def test_get_active_executions_filters(self):
        grid = self._make_grid()
        running_id = uuid4()
        pending_id = uuid4()
        completed_id = uuid4()

        grid._executions[running_id] = AgentExecution(
            id=running_id,
            repo_url="https://github.com/test/repo.git",
            status=ExecutionStatus.RUNNING,
            prompt="test",
        )
        grid._executions[pending_id] = AgentExecution(
            id=pending_id,
            repo_url="https://github.com/test/repo.git",
            status=ExecutionStatus.PENDING,
            prompt="test",
        )
        grid._executions[completed_id] = AgentExecution(
            id=completed_id,
            repo_url="https://github.com/test/repo.git",
            status=ExecutionStatus.COMPLETED,
            prompt="test",
        )

        active = grid.get_active_executions()
        active_ids = {e.id for e in active}
        assert running_id in active_ids
        assert pending_id in active_ids
        assert completed_id not in active_ids

    @pytest.mark.asyncio
    async def test_handle_agent_result_no_callbacks(self):
        """Runs without error when no callbacks are configured."""
        grid = self._make_grid()
        exec_id = uuid4()

        grid._executions[exec_id] = AgentExecution(
            id=exec_id,
            repo_url="https://github.com/test/repo.git",
            status=ExecutionStatus.RUNNING,
            prompt="test",
        )

        # No callbacks set -- should not raise
        await grid.handle_agent_result(
            execution_id=exec_id,
            status="completed",
            result="Done",
        )

        assert exec_id not in grid._executions

    @pytest.mark.asyncio
    async def test_close_cancels_all(self):
        """close() should cancel all tracked machines."""
        grid = self._make_grid()
        ids = [uuid4() for _ in range(3)]

        for eid in ids:
            grid._executions[eid] = AgentExecution(
                id=eid,
                repo_url="https://github.com/test/repo.git",
                status=ExecutionStatus.RUNNING,
                prompt="test",
            )
            grid._machine_map[eid] = f"machine-{eid}"

        with patch("agent_grid.fly.machines.get_fly_client") as mock_fly:
            mock_client = MagicMock()
            mock_client.destroy_machine = AsyncMock()
            mock_fly.return_value = mock_client

            await grid.close()

        assert len(grid._executions) == 0
        assert len(grid._machine_map) == 0


class TestRunArtifacts:
    def test_defaults(self):
        a = RunArtifacts()
        assert a.branch is None
        assert a.pr_url is None
        assert a.pr_number is None
        assert a.result is None
        assert a.cost_usd is None
        assert a.session_id is None
        assert a.session_s3_key is None

    def test_with_values(self):
        a = RunArtifacts(
            branch="fix/42",
            pr_url="https://github.com/test/repo/pull/7",
            pr_number=7,
            result="All done",
            cost_usd=2.50,
            session_id="sess-123",
            session_s3_key="sessions/sess-123.json",
        )
        assert a.branch == "fix/42"
        assert a.pr_number == 7
        assert a.cost_usd == 2.50


class TestClaudeCodeCallbacks:
    def test_defaults(self):
        c = ClaudeCodeCallbacks()
        assert c.on_execution_completed is None
        assert c.on_execution_failed is None

    def test_with_callbacks(self):
        completed = AsyncMock()
        failed = AsyncMock()
        c = ClaudeCodeCallbacks(
            on_execution_completed=completed,
            on_execution_failed=failed,
        )
        assert c.on_execution_completed is completed
        assert c.on_execution_failed is failed
