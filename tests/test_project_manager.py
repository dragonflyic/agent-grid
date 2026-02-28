"""Tests for GitHub Projects v2 integration (ProjectManager)."""

import pytest

from agent_grid.issue_tracker.project_manager import ProjectManager


class FakeGraphQLResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeHTTPClient:
    """Captures GraphQL calls and returns canned responses."""

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self.calls: list[dict] = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, "json": kwargs.get("json", {})})
        if self._responses:
            return FakeGraphQLResponse(self._responses.pop(0))
        return FakeGraphQLResponse({"data": None})

    async def aclose(self):
        pass


def _make_project_manager(
    responses=None,
    project_number=42,
    project_owner="myorg",
    label_map=None,
) -> tuple[ProjectManager, FakeHTTPClient]:
    """Build a ProjectManager with injected fakes."""
    from agent_grid.config import settings

    old_number = settings.github_project_number
    old_owner = settings.github_project_owner
    old_map = settings.github_project_label_status_map

    settings.github_project_number = project_number
    settings.github_project_owner = project_owner
    if label_map is not None:
        settings.github_project_label_status_map = label_map

    pm = ProjectManager.__new__(ProjectManager)
    http = FakeHTTPClient(responses=responses)
    pm._client = http
    pm._project_number = project_number
    pm._project_owner = project_owner
    pm._project_id = None
    pm._status_field_id = None
    pm._status_options = {}
    pm._initialized = False
    pm._label_status_map = pm._parse_label_map()

    # Restore settings after constructing
    settings.github_project_number = old_number
    settings.github_project_owner = old_owner
    settings.github_project_label_status_map = old_map

    return pm, http


class TestProjectManager:
    def test_not_configured_when_no_project_number(self):
        pm = ProjectManager.__new__(ProjectManager)
        pm._project_number = None
        pm._project_owner = "myorg"
        assert not pm.is_configured()

    @pytest.mark.asyncio
    async def test_sync_status_noop_when_unconfigured(self):
        pm, http = _make_project_manager(project_number=None)
        await pm.sync_status("NODE123", "ag/in-progress")
        assert len(http.calls) == 0

    @pytest.mark.asyncio
    async def test_sync_status_noop_when_no_node_id(self):
        pm, http = _make_project_manager()
        await pm.sync_status(None, "ag/in-progress")
        assert len(http.calls) == 0

    @pytest.mark.asyncio
    async def test_sync_status_noop_for_unmapped_label(self):
        pm, http = _make_project_manager()
        await pm.sync_status("NODE123", "ag/sub-issue")
        assert len(http.calls) == 0

    @pytest.mark.asyncio
    async def test_add_item_and_set_status(self):
        # Response 1: org project query (initialization)
        init_response = {
            "data": {
                "organization": {
                    "projectV2": {
                        "id": "PVT_123",
                        "fields": {
                            "nodes": [
                                {
                                    "id": "FIELD_1",
                                    "name": "Status",
                                    "options": [
                                        {"id": "OPT_TODO", "name": "Todo"},
                                        {"id": "OPT_IP", "name": "In Progress"},
                                        {"id": "OPT_DONE", "name": "Done"},
                                    ],
                                }
                            ]
                        },
                    }
                }
            }
        }
        # Response 2: addProjectV2ItemById
        add_response = {
            "data": {
                "addProjectV2ItemById": {
                    "item": {"id": "PVTI_456"}
                }
            }
        }
        # Response 3: updateProjectV2ItemFieldValue
        update_response = {
            "data": {
                "updateProjectV2ItemFieldValue": {
                    "projectV2Item": {"id": "PVTI_456"}
                }
            }
        }

        pm, http = _make_project_manager(
            responses=[init_response, add_response, update_response],
        )
        await pm.sync_status("NODE_ISSUE_1", "ag/in-progress")

        # Should have made 3 GraphQL calls: init, add, update
        assert len(http.calls) == 3
        # Verify the add mutation used the right content ID
        add_call = http.calls[1]["json"]
        assert add_call["variables"]["contentId"] == "NODE_ISSUE_1"
        # Verify the update mutation used the right option
        update_call = http.calls[2]["json"]
        assert update_call["variables"]["optionId"] == "OPT_IP"

    @pytest.mark.asyncio
    async def test_falls_back_to_user_project(self):
        # Response 1: org query returns no project
        org_response = {"data": {"organization": None}}
        # Response 2: user query returns project
        user_response = {
            "data": {
                "user": {
                    "projectV2": {
                        "id": "PVT_USER",
                        "fields": {"nodes": []},
                    }
                }
            }
        }
        # Response 3: add item
        add_response = {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_789"}}}}

        pm, http = _make_project_manager(
            responses=[org_response, user_response, add_response],
        )
        item_id = await pm.add_item_to_project("NODE_X")

        assert item_id == "PVTI_789"
        # 3 calls: org query, user query, add mutation
        assert len(http.calls) == 3

    @pytest.mark.asyncio
    async def test_graphql_error_returns_none(self):
        error_response = {
            "errors": [{"message": "Something went wrong"}],
        }
        pm, http = _make_project_manager(responses=[error_response])
        result = await pm._ensure_initialized()
        assert result is False

    @pytest.mark.asyncio
    async def test_set_item_status_skips_unknown_status(self):
        pm, http = _make_project_manager()
        # Pre-initialize to avoid init call
        pm._initialized = True
        pm._project_id = "PVT_123"
        pm._status_field_id = "FIELD_1"
        pm._status_options = {"Todo": "OPT_TODO"}

        await pm.set_item_status("PVTI_1", "NonExistent")
        assert len(http.calls) == 0
