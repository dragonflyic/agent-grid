"""Tests for status comment manager."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_grid.coordinator.status_comment import (
    MARKER,
    METADATA_KEY,
    StatusCommentManager,
    _extract_comment_id,
    _render_status,
    _render_status_body,
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


class TestRenderStatusBody:
    """Tests for the body-only renderer (no marker)."""

    def test_no_marker(self):
        body = _render_status_body("launched")
        assert MARKER not in body
        assert "<!-- agent-grid" not in body

    def test_launched_status(self):
        body = _render_status_body("launched")
        assert "Working on it" in body

    def test_custom_detail(self):
        body = _render_status_body("pr_created", "PR #42 created.")
        assert "PR #42 created." in body


class TestExtractCommentId:
    """Tests for extracting comment ID from various metadata shapes."""

    def test_dict_metadata(self):
        assert _extract_comment_id({METADATA_KEY: "123"}) == "123"

    def test_list_metadata(self):
        metadata = [None, {"confidence_score": 9}, {METADATA_KEY: "456"}]
        assert _extract_comment_id(metadata) == "456"

    def test_none_metadata(self):
        assert _extract_comment_id(None) is None

    def test_empty_dict(self):
        assert _extract_comment_id({}) is None

    def test_empty_list(self):
        assert _extract_comment_id([]) is None

    def test_string_json_dict(self):
        import json

        assert _extract_comment_id(json.dumps({METADATA_KEY: "789"})) == "789"


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
            # The slot system uses <!-- agent-grid:status --> marker
            assert "<!-- agent-grid:status -->" in call_args[0][2]

            mock_db.merge_issue_metadata.assert_called_once_with(
                issue_number=42,
                repo="owner/repo",
                metadata_update={"comment_id:status": "12345"},
            )

    @pytest.mark.asyncio
    async def test_updates_existing_comment(self, mock_tracker, mock_db):
        """When comment ID exists in metadata, updates instead of creating."""
        mock_db.get_issue_state.return_value = {
            "metadata": {"comment_id:status": "99999"},
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
            assert "<!-- agent-grid:status -->" in call_args[0][2]

            # Should NOT create a new comment
            mock_tracker.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_backward_compat_legacy_metadata_key(self, mock_tracker, mock_db):
        """Legacy status_comment_id key is used as fallback for 'status' slot."""
        mock_db.get_issue_state.return_value = {
            "metadata": {METADATA_KEY: "77777"},
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
    async def test_falls_back_to_new_comment_on_update_failure(self, mock_tracker, mock_db):
        """If update fails (comment deleted), creates a new one."""
        mock_db.get_issue_state.return_value = {
            "metadata": {"comment_id:status": "deleted_id"},
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
        """Metadata stored as JSON string is parsed correctly (legacy compat)."""
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
    async def test_handles_list_metadata(self, mock_tracker, mock_db):
        """Metadata stored as a list (JSONB concat artifact) is parsed correctly (legacy compat)."""
        mock_db.get_issue_state.return_value = {
            "metadata": [None, {"confidence_score": 9}, {METADATA_KEY: "88888"}],
        }

        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            await mgr.post_or_update("owner/repo", "42", "pr_created")

            mock_tracker.update_comment.assert_called_once()
            assert mock_tracker.update_comment.call_args[0][1] == "88888"
            mock_tracker.add_comment.assert_not_called()

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


class TestPostOrUpdateSlot:
    """Tests for the generic slot-based comment posting."""

    @pytest.fixture
    def mock_tracker(self):
        tracker = AsyncMock()
        tracker.add_comment = AsyncMock(return_value="55555")
        tracker.update_comment = AsyncMock()
        return tracker

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.get_issue_state = AsyncMock(return_value=None)
        db.merge_issue_metadata = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_creates_new_slot_comment(self, mock_tracker, mock_db):
        """Creates a new comment for a slot and stores the ID."""
        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            await mgr.post_or_update_slot("owner/repo", "42", "skip-reason", "Skipping: too vague")

            mock_tracker.add_comment.assert_called_once()
            body = mock_tracker.add_comment.call_args[0][2]
            assert "<!-- agent-grid:skip-reason -->" in body
            assert "Skipping: too vague" in body

            mock_db.merge_issue_metadata.assert_called_once_with(
                issue_number=42,
                repo="owner/repo",
                metadata_update={"comment_id:skip-reason": "55555"},
            )

    @pytest.mark.asyncio
    async def test_updates_existing_slot_comment(self, mock_tracker, mock_db):
        """Updates in-place when slot already has a comment ID."""
        mock_db.get_issue_state.return_value = {
            "metadata": {"comment_id:ci-status": "66666"},
        }

        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            await mgr.post_or_update_slot("owner/repo", "42", "ci-status", "CI still failing")

            mock_tracker.update_comment.assert_called_once()
            body = mock_tracker.update_comment.call_args[0][2]
            assert "<!-- agent-grid:ci-status -->" in body
            assert "CI still failing" in body
            mock_tracker.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_different_slots_are_independent(self, mock_tracker, mock_db):
        """Different slots don't interfere with each other."""
        mock_db.get_issue_state.return_value = {
            "metadata": {"comment_id:status": "11111"},
        }

        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            # Posting to "skip-reason" slot should NOT find the "status" slot comment
            await mgr.post_or_update_slot("owner/repo", "42", "skip-reason", "test body")

            # Should create new, not update
            mock_tracker.add_comment.assert_called_once()
            mock_tracker.update_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_slot_falls_back_on_update_failure(self, mock_tracker, mock_db):
        """If updating a slot comment fails, creates a new one."""
        mock_db.get_issue_state.return_value = {
            "metadata": {"comment_id:completion": "deleted_id"},
        }
        mock_tracker.update_comment.side_effect = Exception("Not Found")

        with (
            patch("agent_grid.coordinator.status_comment.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.status_comment.get_database", return_value=mock_db),
        ):
            mgr = StatusCommentManager()
            await mgr.post_or_update_slot("owner/repo", "42", "completion", "All done!")

            mock_tracker.update_comment.assert_called_once()
            mock_tracker.add_comment.assert_called_once()
