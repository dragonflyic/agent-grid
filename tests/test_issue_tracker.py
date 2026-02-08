"""Tests for issue tracker module."""

import pytest

from agent_grid.issue_tracker import IssueInfo, IssueStatus
from agent_grid.issue_tracker.filesystem_client import FilesystemClient


class TestIssueInfo:
    """Tests for IssueInfo model."""

    def test_create_issue_info(self):
        """Test creating an IssueInfo."""
        issue = IssueInfo(
            id="123",
            number=123,
            title="Test Issue",
            body="Issue description",
            repo_url="https://github.com/test/repo",
            html_url="https://github.com/test/repo/issues/123",
        )
        assert issue.id == "123"
        assert issue.number == 123
        assert issue.status == IssueStatus.OPEN
        assert issue.labels == []

    def test_issue_with_labels(self):
        """Test issue with labels."""
        issue = IssueInfo(
            id="456",
            number=456,
            title="Bug",
            repo_url="https://github.com/test/repo",
            html_url="https://github.com/test/repo/issues/456",
            labels=["bug", "high-priority"],
        )
        assert "bug" in issue.labels
        assert "high-priority" in issue.labels

    def test_issue_with_parent(self):
        """Test issue with parent reference."""
        issue = IssueInfo(
            id="789",
            number=789,
            title="Subtask",
            repo_url="https://github.com/test/repo",
            html_url="https://github.com/test/repo/issues/789",
            parent_id="100",
        )
        assert issue.parent_id == "100"


class TestWebhookHandler:
    """Tests for webhook handler."""

    def test_verify_signature_valid(self):
        """Test signature verification with valid signature."""
        import hashlib
        import hmac

        from agent_grid.issue_tracker.webhook_handler import verify_signature

        secret = "test_secret"
        payload = b'{"test": "data"}'
        expected = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()

        assert verify_signature(payload, f"sha256={expected}", secret) is True

    def test_verify_signature_invalid(self):
        """Test signature verification with invalid signature."""
        from agent_grid.issue_tracker.webhook_handler import verify_signature

        payload = b'{"test": "data"}'
        assert verify_signature(payload, "sha256=invalid", "test_secret") is False

    def test_verify_signature_missing(self):
        """Test signature verification with missing signature."""
        from agent_grid.issue_tracker.webhook_handler import verify_signature

        payload = b'{"test": "data"}'
        assert verify_signature(payload, None, "test_secret") is False

    def test_verify_signature_wrong_format(self):
        """Test signature verification with wrong format."""
        from agent_grid.issue_tracker.webhook_handler import verify_signature

        payload = b'{"test": "data"}'
        assert verify_signature(payload, "md5=something", "test_secret") is False


class TestFilesystemClient:
    """Tests for FilesystemClient."""

    @pytest.fixture
    def client(self, tmp_path):
        """Create a filesystem client with temp directory."""
        return FilesystemClient(issues_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_create_issue(self, client):
        """Test creating an issue."""
        issue = await client.create_issue(
            repo="test/repo",
            title="Test Issue",
            body="This is a test issue.",
            labels=["bug", "urgent"],
        )

        assert issue.id == "1"
        assert issue.number == 1
        assert issue.title == "Test Issue"
        assert issue.body == "This is a test issue."
        assert issue.status == IssueStatus.OPEN
        assert "bug" in issue.labels
        assert "urgent" in issue.labels

    @pytest.mark.asyncio
    async def test_get_issue(self, client):
        """Test retrieving an issue."""
        created = await client.create_issue(
            repo="test/repo",
            title="Test Issue",
            body="Test body",
        )

        retrieved = await client.get_issue("test/repo", created.id)

        assert retrieved.id == created.id
        assert retrieved.title == created.title
        assert retrieved.body == created.body

    @pytest.mark.asyncio
    async def test_issue_not_found(self, client):
        """Test getting non-existent issue raises error."""
        with pytest.raises(FileNotFoundError):
            await client.get_issue("test/repo", "999")

    @pytest.mark.asyncio
    async def test_add_comment(self, client):
        """Test adding a comment to an issue."""
        issue = await client.create_issue(
            repo="test/repo",
            title="Test Issue",
            body="Test body",
        )

        await client.add_comment("test/repo", issue.id, "First comment")
        await client.add_comment("test/repo", issue.id, "Second comment")

        updated = await client.get_issue("test/repo", issue.id)

        assert len(updated.comments) == 2
        assert updated.comments[0].body == "First comment"
        assert updated.comments[1].body == "Second comment"

    @pytest.mark.asyncio
    async def test_update_status(self, client):
        """Test updating issue status."""
        issue = await client.create_issue(
            repo="test/repo",
            title="Test Issue",
            body="Test body",
        )

        await client.update_issue_status("test/repo", issue.id, IssueStatus.IN_PROGRESS)
        updated = await client.get_issue("test/repo", issue.id)

        assert updated.status == IssueStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_create_subissue(self, client):
        """Test creating a subissue."""
        parent = await client.create_issue(
            repo="test/repo",
            title="Parent Issue",
            body="Parent body",
        )

        child = await client.create_subissue(
            repo="test/repo",
            parent_id=parent.id,
            title="Child Issue",
            body="Child body",
        )

        assert child.parent_id == parent.id

    @pytest.mark.asyncio
    async def test_list_subissues(self, client):
        """Test listing subissues of a parent."""
        parent = await client.create_issue(
            repo="test/repo",
            title="Parent Issue",
            body="Parent body",
        )

        await client.create_subissue(
            repo="test/repo",
            parent_id=parent.id,
            title="Child 1",
            body="Child 1 body",
        )
        await client.create_subissue(
            repo="test/repo",
            parent_id=parent.id,
            title="Child 2",
            body="Child 2 body",
        )

        subissues = await client.list_subissues("test/repo", parent.id)

        assert len(subissues) == 2
        assert all(s.parent_id == parent.id for s in subissues)

    @pytest.mark.asyncio
    async def test_blocked_by_relationship(self, client):
        """Test blocked_by relationship."""
        blocker = await client.create_issue(
            repo="test/repo",
            title="Blocker Issue",
            body="This blocks other issues",
        )

        blocked = await client.create_issue(
            repo="test/repo",
            title="Blocked Issue",
            body="This is blocked",
            blocked_by=[blocker.id],
        )

        assert blocker.id in blocked.blocked_by

        # Check is_blocked
        is_blocked = await client.is_blocked("test/repo", blocked.id)
        assert is_blocked is True

        # Close the blocker
        await client.update_issue_status("test/repo", blocker.id, IssueStatus.CLOSED)

        # Should no longer be blocked
        is_blocked = await client.is_blocked("test/repo", blocked.id)
        assert is_blocked is False

    @pytest.mark.asyncio
    async def test_get_blocked_issues(self, client):
        """Test getting issues blocked by a specific issue."""
        blocker = await client.create_issue(
            repo="test/repo",
            title="Blocker",
            body="Blocker body",
        )

        await client.create_issue(
            repo="test/repo",
            title="Blocked 1",
            body="Blocked 1 body",
            blocked_by=[blocker.id],
        )
        await client.create_issue(
            repo="test/repo",
            title="Blocked 2",
            body="Blocked 2 body",
            blocked_by=[blocker.id],
        )
        await client.create_issue(
            repo="test/repo",
            title="Not blocked",
            body="Not blocked body",
        )

        blocked = await client.get_blocked_issues("test/repo", blocker.id)

        assert len(blocked) == 2
        assert all(blocker.id in b.blocked_by for b in blocked)

    @pytest.mark.asyncio
    async def test_list_issues(self, client):
        """Test listing all issues."""
        await client.create_issue(
            repo="test/repo",
            title="Issue 1",
            body="Body 1",
            labels=["bug"],
        )
        await client.create_issue(
            repo="test/repo",
            title="Issue 2",
            body="Body 2",
            labels=["feature"],
        )
        await client.create_issue(
            repo="test/repo",
            title="Issue 3",
            body="Body 3",
            labels=["bug"],
        )

        # List all
        all_issues = await client.list_issues("test/repo")
        assert len(all_issues) == 3

        # Filter by label
        bugs = await client.list_issues("test/repo", labels=["bug"])
        assert len(bugs) == 2

    @pytest.mark.asyncio
    async def test_update_issue(self, client):
        """Test updating issue fields."""
        issue = await client.create_issue(
            repo="test/repo",
            title="Original Title",
            body="Original body",
        )

        updated = await client.update_issue(
            repo="test/repo",
            issue_id=issue.id,
            title="Updated Title",
            labels=["new-label"],
        )

        assert updated.title == "Updated Title"
        assert "new-label" in updated.labels
        assert updated.body == "Original body"  # Unchanged

    @pytest.mark.asyncio
    async def test_auto_increment_ids(self, client):
        """Test that issue IDs auto-increment."""
        issue1 = await client.create_issue(
            repo="test/repo",
            title="Issue 1",
            body="Body 1",
        )
        issue2 = await client.create_issue(
            repo="test/repo",
            title="Issue 2",
            body="Body 2",
        )
        issue3 = await client.create_issue(
            repo="test/repo",
            title="Issue 3",
            body="Body 3",
        )

        assert issue1.id == "1"
        assert issue2.id == "2"
        assert issue3.id == "3"
