"""Tests for GitHub client implementation."""

import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from agent_grid.issue_tracker import IssueStatus
from agent_grid.issue_tracker.github_client import GitHubClient


class TestGitHubClientParsing:
    """Tests for GitHubClient parsing logic (no API calls)."""

    @pytest.fixture
    def client(self):
        """Create a client with dummy token."""
        with patch.object(GitHubClient, "__init__", lambda self, token=None: None):
            client = GitHubClient.__new__(GitHubClient)
            client._token = "dummy"
            client._client = None
            # Set up the patterns
            import re
            client.PARENT_PATTERN = re.compile(r"^Parent:\s*#(\d+)\s*$", re.MULTILINE)
            client.BLOCKED_BY_PATTERN = re.compile(r"^Blocked by:\s*(.+)$", re.MULTILINE)
            client.ISSUE_REF_PATTERN = re.compile(r"#(\d+)")
            client.IN_PROGRESS_LABEL = "in-progress"
            client.SUBISSUE_LABEL = "subissue"
            return client

    def test_parse_issue_basic(self, client):
        """Test parsing a basic issue."""
        data = {
            "number": 1,
            "title": "Test Issue",
            "body": "Issue description",
            "state": "open",
            "labels": [],
            "html_url": "https://github.com/test/repo/issues/1",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
        }

        issue = client._parse_issue("test/repo", data)

        assert issue.id == "1"
        assert issue.number == 1
        assert issue.title == "Test Issue"
        assert issue.body == "Issue description"
        assert issue.status == IssueStatus.OPEN
        assert issue.parent_id is None
        assert issue.blocked_by == []

    def test_parse_issue_with_parent(self, client):
        """Test parsing issue with parent reference."""
        data = {
            "number": 2,
            "title": "Child Issue",
            "body": "Parent: #1\n\nChild description",
            "state": "open",
            "labels": [{"name": "subissue"}],
            "html_url": "https://github.com/test/repo/issues/2",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
        }

        issue = client._parse_issue("test/repo", data)

        assert issue.parent_id == "1"
        assert issue.body == "Child description"

    def test_parse_issue_with_blocked_by(self, client):
        """Test parsing issue with blocked_by references."""
        data = {
            "number": 3,
            "title": "Blocked Issue",
            "body": "Blocked by: #1, #2\n\nThis is blocked",
            "state": "open",
            "labels": [],
            "html_url": "https://github.com/test/repo/issues/3",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
        }

        issue = client._parse_issue("test/repo", data)

        assert issue.blocked_by == ["1", "2"]
        assert issue.body == "This is blocked"

    def test_parse_issue_with_all_metadata(self, client):
        """Test parsing issue with parent and blocked_by."""
        data = {
            "number": 4,
            "title": "Complex Issue",
            "body": "Parent: #1\nBlocked by: #2, #3\n\nActual description",
            "state": "open",
            "labels": [{"name": "subissue"}],
            "html_url": "https://github.com/test/repo/issues/4",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
        }

        issue = client._parse_issue("test/repo", data)

        assert issue.parent_id == "1"
        assert issue.blocked_by == ["2", "3"]
        assert issue.body == "Actual description"

    def test_parse_issue_in_progress(self, client):
        """Test parsing issue with in-progress label."""
        data = {
            "number": 5,
            "title": "WIP Issue",
            "body": "Work in progress",
            "state": "open",
            "labels": [{"name": "in-progress"}],
            "html_url": "https://github.com/test/repo/issues/5",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
        }

        issue = client._parse_issue("test/repo", data)

        assert issue.status == IssueStatus.IN_PROGRESS

    def test_parse_issue_closed(self, client):
        """Test parsing closed issue."""
        data = {
            "number": 6,
            "title": "Done Issue",
            "body": "Completed",
            "state": "closed",
            "labels": [],
            "html_url": "https://github.com/test/repo/issues/6",
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z",
        }

        issue = client._parse_issue("test/repo", data)

        assert issue.status == IssueStatus.CLOSED

    def test_build_body_basic(self, client):
        """Test building body without metadata."""
        body = client._build_body("Description", None, None)
        assert body == "Description"

    def test_build_body_with_parent(self, client):
        """Test building body with parent."""
        body = client._build_body("Description", "1", None)
        assert body == "Parent: #1\n\nDescription"

    def test_build_body_with_blocked_by(self, client):
        """Test building body with blocked_by."""
        body = client._build_body("Description", None, ["1", "2"])
        assert body == "Blocked by: #1, #2\n\nDescription"

    def test_build_body_with_all(self, client):
        """Test building body with all metadata."""
        body = client._build_body("Description", "1", ["2", "3"])
        assert body == "Parent: #1\nBlocked by: #2, #3\n\nDescription"

    def test_strip_metadata(self, client):
        """Test stripping metadata from body."""
        body = "Parent: #1\nBlocked by: #2, #3\n\nActual content"
        stripped = client._strip_metadata(body)
        assert stripped == "Actual content"

    def test_strip_metadata_preserves_content(self, client):
        """Test that strip_metadata preserves non-metadata content."""
        body = "No metadata here\nJust content"
        stripped = client._strip_metadata(body)
        assert stripped == "No metadata here\nJust content"


# Integration tests - only run if AGENT_GRID_GITHUB_TOKEN is set
# and AGENT_GRID_TEST_REPO is set (e.g., "owner/repo")
@pytest.mark.skipif(
    not os.environ.get("AGENT_GRID_GITHUB_TOKEN") or not os.environ.get("AGENT_GRID_TEST_REPO"),
    reason="GitHub integration tests require AGENT_GRID_GITHUB_TOKEN and AGENT_GRID_TEST_REPO env vars",
)
class TestGitHubClientIntegration:
    """Integration tests for GitHubClient against real GitHub API."""

    @pytest.fixture
    def client(self):
        """Create a client with real token."""
        return GitHubClient(token=os.environ["AGENT_GRID_GITHUB_TOKEN"])

    @pytest.fixture
    def repo(self):
        """Get test repo from environment."""
        return os.environ["AGENT_GRID_TEST_REPO"]

    @pytest.mark.asyncio
    async def test_create_and_get_issue(self, client, repo):
        """Test creating and retrieving an issue."""
        try:
            # Create issue
            issue = await client.create_issue(
                repo=repo,
                title="[TEST] Integration test issue",
                body="This is a test issue created by integration tests.",
                labels=["test"],
            )

            assert issue.title == "[TEST] Integration test issue"
            assert issue.status == IssueStatus.OPEN

            # Get issue
            retrieved = await client.get_issue(repo, issue.id)
            assert retrieved.id == issue.id
            assert retrieved.title == issue.title

            # Close issue (cleanup)
            await client.update_issue_status(repo, issue.id, IssueStatus.CLOSED)

        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_add_and_get_comments(self, client, repo):
        """Test adding and retrieving comments."""
        try:
            # Create issue
            issue = await client.create_issue(
                repo=repo,
                title="[TEST] Comment test issue",
                body="Testing comments",
            )

            # Add comments
            await client.add_comment(repo, issue.id, "First comment")
            await client.add_comment(repo, issue.id, "Second comment")

            # Get issue with comments
            retrieved = await client.get_issue(repo, issue.id)
            assert len(retrieved.comments) == 2
            assert retrieved.comments[0].body == "First comment"
            assert retrieved.comments[1].body == "Second comment"

            # Cleanup
            await client.update_issue_status(repo, issue.id, IssueStatus.CLOSED)

        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_subissue_workflow(self, client, repo):
        """Test creating and listing subissues."""
        try:
            # Create parent
            parent = await client.create_issue(
                repo=repo,
                title="[TEST] Parent issue",
                body="Parent for subissue test",
            )

            # Create subissue
            child = await client.create_subissue(
                repo=repo,
                parent_id=parent.id,
                title="[TEST] Child issue",
                body="Child of parent",
            )

            assert child.parent_id == parent.id

            # List subissues
            subissues = await client.list_subissues(repo, parent.id)
            assert len(subissues) >= 1
            assert any(s.id == child.id for s in subissues)

            # Cleanup
            await client.update_issue_status(repo, child.id, IssueStatus.CLOSED)
            await client.update_issue_status(repo, parent.id, IssueStatus.CLOSED)

        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_blocked_by_workflow(self, client, repo):
        """Test blocked_by relationship."""
        try:
            # Create blocker
            blocker = await client.create_issue(
                repo=repo,
                title="[TEST] Blocker issue",
                body="This blocks other issues",
            )

            # Create blocked issue
            blocked = await client.create_issue(
                repo=repo,
                title="[TEST] Blocked issue",
                body="This is blocked",
                blocked_by=[blocker.id],
            )

            assert blocker.id in blocked.blocked_by

            # Check is_blocked
            is_blocked = await client.is_blocked(repo, blocked.id)
            assert is_blocked is True

            # Close blocker
            await client.update_issue_status(repo, blocker.id, IssueStatus.CLOSED)

            # Should no longer be blocked
            is_blocked = await client.is_blocked(repo, blocked.id)
            assert is_blocked is False

            # Cleanup
            await client.update_issue_status(repo, blocked.id, IssueStatus.CLOSED)

        finally:
            await client.close()
