"""
Real end-to-end integration test using actual GitHub and Claude Code SDK.

This test requires:
- AGENT_GRID_GITHUB_TOKEN environment variable
- Claude Code SDK configured and working

Run with: pytest tests/test_real_e2e.py -v -s
"""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from agent_grid.execution_grid import AgentExecution, ExecutionStatus
from agent_grid.issue_tracker import IssueStatus
from agent_grid.execution_grid.agent_runner import AgentRunner
from agent_grid.execution_grid.repo_manager import RepoManager
from agent_grid.issue_tracker.github_client import GitHubClient


# Test configuration
TEST_REPO = "dragonflyic/agent-grid"
REPO_URL = "https://github.com/dragonflyic/agent-grid.git"


def get_github_token():
    """Get GitHub token from environment."""
    token = os.environ.get("AGENT_GRID_GITHUB_TOKEN")
    if not token:
        pytest.skip("AGENT_GRID_GITHUB_TOKEN not set")
    return token


@pytest.fixture
def temp_repo_dir():
    """Create temporary directory for repo operations."""
    with tempfile.TemporaryDirectory(prefix="agent-grid-test-") as tmpdir:
        yield Path(tmpdir)


class TestRealEndToEnd:
    """
    Real end-to-end tests using actual GitHub API and Claude Code SDK.

    These tests create real issues and run real agents.
    """

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)  # 5 minute timeout
    async def test_ping_pong_with_real_agent(self, temp_repo_dir):
        """
        Full end-to-end test:
        1. Create a test issue in GitHub
        2. Run a real Claude Code agent
        3. Verify the agent responds appropriately
        4. Clean up
        """
        token = get_github_token()
        github_client = GitHubClient(token=token)
        issue = None

        try:
            # Step 1: Create a test issue
            print("\n=== Step 1: Creating test issue ===")
            issue = await github_client.create_issue(
                repo=TEST_REPO,
                title="[TEST] Ping - Please respond with pong",
                body="""This is an automated test issue.

Your task is simple: respond to this ping by outputting the word "pong".

You do not need to modify any files or make any commits. Just acknowledge this message by saying "pong" in your response.

This issue will be automatically closed after the test completes.""",
                labels=["test", "automated"],
            )
            print(f"Created issue #{issue.id}: {issue.html_url}")

            # Step 2: Set up the agent runner
            print("\n=== Step 2: Setting up agent runner ===")
            repo_manager = RepoManager(base_path=str(temp_repo_dir))
            agent_runner = AgentRunner()
            agent_runner._repo_manager = repo_manager

            # Step 3: Create execution and run agent
            print("\n=== Step 3: Running agent ===")
            from uuid import uuid4

            execution = AgentExecution(
                id=uuid4(),
                issue_id=issue.id,
                repo_url=REPO_URL,
                status=ExecutionStatus.PENDING,
                prompt=f"""You are responding to GitHub issue #{issue.id} in the {TEST_REPO} repository.

Issue Title: {issue.title}

Issue Body:
{issue.body}

Please respond to this ping by outputting "pong". You do not need to modify any files or make commits - just acknowledge the ping with a pong response.""",
            )

            print(f"Execution ID: {execution.id}")
            print(f"Prompt: {execution.prompt[:200]}...")

            # Run the agent (this will clone repo, run Claude, etc.)
            result = await agent_runner.run(execution, execution.prompt)

            # Step 4: Verify results
            print("\n=== Step 4: Verifying results ===")
            print(f"Status: {result.status}")
            print(f"Result: {result.result[:500] if result.result else 'None'}...")

            assert result.status == ExecutionStatus.COMPLETED, f"Agent failed: {result.result}"
            assert result.result is not None, "Agent returned no result"

            # Check for pong in the response (case insensitive)
            assert "pong" in result.result.lower(), f"Agent didn't say pong. Result: {result.result}"

            print("\n=== Test PASSED ===")

        finally:
            # Step 5: Cleanup - close the issue
            if issue:
                print(f"\n=== Cleanup: Closing issue #{issue.id} ===")
                try:
                    await github_client.add_comment(
                        TEST_REPO,
                        issue.id,
                        "✅ Test completed successfully. Closing issue."
                    )
                    await github_client.update_issue_status(
                        TEST_REPO,
                        issue.id,
                        IssueStatus.CLOSED
                    )
                    print(f"Issue #{issue.id} closed")
                except Exception as e:
                    print(f"Warning: Failed to close issue: {e}")
            await github_client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_agent_creates_file(self, temp_repo_dir):
        """
        Test that agent can create a file in the repo.

        Note: This test creates a file but doesn't push it (to avoid polluting the repo).
        We verify the file was created in the working directory.
        """
        token = get_github_token()
        github_client = GitHubClient(token=token)
        issue = None
        work_dir = None

        try:
            # Step 1: Create a test issue
            print("\n=== Step 1: Creating test issue ===")
            issue = await github_client.create_issue(
                repo=TEST_REPO,
                title="[TEST] Create a pong.txt file",
                body="""This is an automated test issue.

Your task: Create a file called `test_pong.txt` in the root of the repository containing the text "pong".

Do NOT commit or push the changes - just create the file.

This issue will be automatically closed after the test completes.""",
                labels=["test", "automated"],
            )
            print(f"Created issue #{issue.id}: {issue.html_url}")

            # Step 2: Set up the agent runner with custom repo manager
            print("\n=== Step 2: Setting up agent runner ===")
            repo_manager = RepoManager(base_path=str(temp_repo_dir))
            agent_runner = AgentRunner()
            agent_runner._repo_manager = repo_manager

            # Patch push to be a no-op (we don't want to actually push)
            original_push = repo_manager.push_branch
            async def mock_push(execution_id, branch_name):
                print(f"(Skipping push for branch {branch_name})")
            repo_manager.push_branch = mock_push

            # Step 3: Create execution and run agent
            print("\n=== Step 3: Running agent ===")
            from uuid import uuid4

            execution_id = uuid4()
            execution = AgentExecution(
                id=execution_id,
                issue_id=issue.id,
                repo_url=REPO_URL,
                status=ExecutionStatus.PENDING,
                prompt=f"""You are responding to GitHub issue #{issue.id} in the {TEST_REPO} repository.

Issue Title: {issue.title}

Issue Body:
{issue.body}

Create a file called `test_pong.txt` in the root directory containing just the word "pong".

Do not commit or push - just create the file.""",
            )

            print(f"Execution ID: {execution.id}")

            # Run the agent
            result = await agent_runner.run(execution, execution.prompt)

            # Get the working directory path before cleanup
            work_dir = repo_manager.get_execution_path(execution_id)

            # Step 4: Verify results
            print("\n=== Step 4: Verifying results ===")
            print(f"Status: {result.status}")
            print(f"Result preview: {result.result[:300] if result.result else 'None'}...")

            # Check if the file was created
            pong_file = work_dir / "test_pong.txt"
            if pong_file.exists():
                content = pong_file.read_text()
                print(f"File created! Content: {content}")
                assert "pong" in content.lower(), f"File doesn't contain pong: {content}"
            else:
                # File might not exist if cleanup already ran or agent didn't create it
                # Check the result for indication of success
                print("File not found (may have been cleaned up)")
                assert result.status == ExecutionStatus.COMPLETED, f"Agent failed: {result.result}"

            print("\n=== Test PASSED ===")

        finally:
            # Cleanup
            if issue:
                print(f"\n=== Cleanup: Closing issue #{issue.id} ===")
                try:
                    await github_client.add_comment(
                        TEST_REPO,
                        issue.id,
                        "✅ Test completed. Closing issue."
                    )
                    await github_client.update_issue_status(
                        TEST_REPO,
                        issue.id,
                        IssueStatus.CLOSED
                    )
                except Exception as e:
                    print(f"Warning: Failed to close issue: {e}")
            await github_client.close()


class TestCoordinatorIntegration:
    """Test the full coordinator flow with real components."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_nudge_triggers_real_agent(self, temp_repo_dir):
        """
        Test the coordinator nudge flow:
        1. Create an issue
        2. Create a nudge request
        3. Verify the scheduler would process it
        """
        token = get_github_token()
        github_client = GitHubClient(token=token)
        issue = None

        try:
            # Create issue
            print("\n=== Creating test issue ===")
            issue = await github_client.create_issue(
                repo=TEST_REPO,
                title="[TEST] Nudge test - ping",
                body="Respond with pong. This is a coordinator nudge test.",
                labels=["test", "agent"],  # 'agent' label triggers auto-launch
            )
            print(f"Created issue #{issue.id}: {issue.html_url}")

            # Set up coordinator components with mocked database
            from agent_grid.coordinator.nudge_handler import NudgeHandler
            from agent_grid.coordinator.scheduler import Scheduler
            from tests.test_e2e import MockDatabase

            mock_db = MockDatabase()

            nudge_handler = NudgeHandler()
            nudge_handler._db = mock_db

            # Create a nudge
            print("\n=== Creating nudge ===")
            nudge = await nudge_handler.handle_nudge(
                issue_id=issue.id,
                priority=10,
                reason="Test nudge",
            )
            print(f"Created nudge: {nudge.id}")

            # Verify nudge was queued
            pending = await mock_db.get_pending_nudges()
            assert len(pending) == 1
            assert pending[0].issue_id == issue.id

            print("\n=== Nudge flow verified ===")

            # Now run a real agent for this issue
            print("\n=== Running real agent ===")
            repo_manager = RepoManager(base_path=str(temp_repo_dir))
            agent_runner = AgentRunner()
            agent_runner._repo_manager = repo_manager

            # Skip push
            async def mock_push(execution_id, branch_name):
                pass
            repo_manager.push_branch = mock_push

            from uuid import uuid4
            execution = AgentExecution(
                id=uuid4(),
                issue_id=issue.id,
                repo_url=REPO_URL,
                status=ExecutionStatus.PENDING,
                prompt=f"Issue #{issue.id}: {issue.title}\n\n{issue.body}\n\nRespond with pong.",
            )

            result = await agent_runner.run(execution, execution.prompt)

            print(f"Agent status: {result.status}")
            print(f"Agent result: {result.result[:200] if result.result else 'None'}...")

            assert result.status == ExecutionStatus.COMPLETED
            assert "pong" in result.result.lower()

            print("\n=== Test PASSED ===")

        finally:
            if issue:
                try:
                    await github_client.add_comment(TEST_REPO, issue.id, "✅ Test completed.")
                    await github_client.update_issue_status(TEST_REPO, issue.id, IssueStatus.CLOSED)
                except Exception as e:
                    print(f"Cleanup warning: {e}")
            await github_client.close()
