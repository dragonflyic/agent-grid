"""Tests for status comment manager."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_grid.coordinator.status_comment import (
    MARKER,
    METADATA_KEY,
    StatusCommentManager,
    _render_status,
)


class TestRenderStatus:
    """Tests for status rendering."""

    def test_includes_marker(self):
        body = _render_status("launched")
        assert MARKER in body

    def test_launched_status(self):
        body = _render_status("launched")
        assert "Working on it" in body

    def test_failed_status(self):
        body = _render_status("failed")
        assert "Failed" in body

    def test_custom_detail(self):
        body = _render_status("pr_created", "PR #42 created.")
        assert "PR #42 created." in body

    def test_unknown_stage_fallback(self):
        body = _render_status("unknown_stage")
        assert "Update" in body


class TestStatusCommentManager:
    """Tests for posting and updating status comments."""

    @pytest.fixture
    def mock_tracker(self):
        tracker = AsyncMock()
        tracker.add_comment = AsyncMock(return_value="12345")
        tracker.update_comment = AsyncMock()
        return tracker

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.get_issue_state = AsyncMock(return_value=None)
        db.merge_issue_metadata = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_creates_new_comment_when_no_existing(self, mock_tracker, mock_db):
        """First call creates a new comment and stores its ID."""
        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            await mgr.post_or_update("owner/repo", "42", "launched")

            mock_tracker.add_comment.assert_called_once()
            call_args = mock_tracker.add_comment.call_args
            assert call_args[0][0] == "owner/repo"
            assert call_args[0][1] == "42"
            assert MARKER in call_args[0][2]

            mock_db.merge_issue_metadata.assert_called_once_with(
                issue_number=42,
                repo="owner/repo",
                metadata_update={METADATA_KEY: "12345"},
            )

    @pytest.mark.asyncio
    async def test_updates_existing_comment(self, mock_tracker, mock_db):
        """When comment ID exists in metadata, updates instead of creating."""
        mock_db.get_issue_state.return_value = {
            "metadata": {METADATA_KEY: "99999"},
        }

        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            await mgr.post_or_update("owner/repo", "42", "review_pending")

            mock_tracker.update_comment.assert_called_once()
            call_args = mock_tracker.update_comment.call_args
            assert call_args[0][0] == "owner/repo"
            assert call_args[0][1] == "99999"
            assert MARKER in call_args[0][2]

            # Should NOT create a new comment
            mock_tracker.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_new_comment_on_update_failure(self, mock_tracker, mock_db):
        """If update fails (comment deleted), creates a new one."""
        mock_db.get_issue_state.return_value = {
            "metadata": {METADATA_KEY: "deleted_id"},
        }
        mock_tracker.update_comment.side_effect = Exception("Not Found")

        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            await mgr.post_or_update("owner/repo", "42", "failed")

            mock_tracker.update_comment.assert_called_once()
            mock_tracker.add_comment.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_string_metadata(self, mock_tracker, mock_db):
        """Metadata stored as JSON string is parsed correctly."""
        import json

        mock_db.get_issue_state.return_value = {
            "metadata": json.dumps({METADATA_KEY: "77777"}),
        }

        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            await mgr.post_or_update("owner/repo", "42", "launched")

            mock_tracker.update_comment.assert_called_once()
            assert mock_tracker.update_comment.call_args[0][1] == "77777"

    @pytest.mark.asyncio
    async def test_no_crash_on_add_comment_failure(self, mock_tracker, mock_db):
        """If add_comment fails, should not raise."""
        mock_tracker.add_comment.side_effect = Exception("API error")

        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            # Should not raise
            await mgr.post_or_update("owner/repo", "42", "launched")
