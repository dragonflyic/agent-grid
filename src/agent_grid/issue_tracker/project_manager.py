"""GitHub Projects v2 integration.

Syncs issue status to a GitHub Project board whenever a label
transition occurs.  All errors are logged and swallowed — this
integration never breaks the main pipeline.
"""

import json
import logging

import httpx

from ..config import settings

logger = logging.getLogger("agent_grid.project_manager")

GRAPHQL_URL = "https://api.github.com/graphql"


class ProjectManager:
    """Manages items on a GitHub Projects v2 board via GraphQL."""

    def __init__(self):
        self._app_auth = None  # lazy-loaded
        self._client = httpx.AsyncClient(
            headers={
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self._project_number = settings.github_project_number
        self._project_owner = settings.github_project_owner
        self._project_id: str | None = None
        self._status_field_id: str | None = None
        self._status_options: dict[str, str] = {}  # display name -> option id
        self._initialized = False
        self._label_status_map: dict[str, str] = self._parse_label_map()

    async def _ensure_auth(self) -> None:
        """Ensure the client has a valid Authorization header."""
        from ..github_app import get_github_app_auth

        if self._app_auth is None:
            self._app_auth = get_github_app_auth()
        token = await self._app_auth.get_installation_token()
        self._client.headers["Authorization"] = f"Bearer {token}"

    @staticmethod
    def _parse_label_map() -> dict[str, str]:
        try:
            return json.loads(settings.github_project_label_status_map)
        except (json.JSONDecodeError, TypeError):
            return {}

    def is_configured(self) -> bool:
        return bool(self._project_number and self._project_owner)

    async def _graphql(self, query: str, variables: dict | None = None) -> dict | None:
        await self._ensure_auth()
        try:
            payload: dict = {"query": query}
            if variables:
                payload["variables"] = variables
            response = await self._client.post(GRAPHQL_URL, json=payload)
            response.raise_for_status()
            result = response.json()
            if result.get("errors"):
                logger.warning(f"GraphQL errors: {result['errors']}")
                return None
            return result.get("data")
        except Exception as e:
            logger.warning(f"GraphQL request failed: {e}")
            return None

    async def _ensure_initialized(self) -> bool:
        """Lazily fetch project ID, Status field ID, and option IDs."""
        if self._initialized:
            return self._project_id is not None

        if not self.is_configured():
            return False

        self._initialized = True
        owner = self._project_owner
        number = self._project_number

        # Try org project first, then user project
        for owner_type in ("organization", "user"):
            query = (
                """
            query($owner: String!, $number: Int!) {
              %s(login: $owner) {
                projectV2(number: $number) {
                  id
                  fields(first: 50) {
                    nodes {
                      ... on ProjectV2SingleSelectField {
                        id
                        name
                        options {
                          id
                          name
                        }
                      }
                    }
                  }
                }
              }
            }
            """
                % owner_type
            )

            data = await self._graphql(query, {"owner": owner, "number": number})
            if not data:
                continue

            project_data = (data.get(owner_type) or {}).get("projectV2")
            if not project_data:
                continue

            self._project_id = project_data["id"]

            # Find the Status field
            for field in project_data.get("fields", {}).get("nodes", []):
                if field.get("name") == "Status":
                    self._status_field_id = field["id"]
                    self._status_options = {opt["name"]: opt["id"] for opt in field.get("options", [])}
                    break

            if self._project_id:
                logger.info(
                    f"ProjectManager initialized: project={self._project_id}, "
                    f"status_options={list(self._status_options.keys())}"
                )
                return True

        logger.warning(f"Could not find project #{number} for {owner}")
        return False

    async def add_item_to_project(self, node_id: str) -> str | None:
        """Add an issue/PR to the project by its node_id. Returns item ID."""
        if not await self._ensure_initialized():
            return None

        mutation = """
        mutation($projectId: ID!, $contentId: ID!) {
          addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
            item { id }
          }
        }
        """
        data = await self._graphql(
            mutation,
            {"projectId": self._project_id, "contentId": node_id},
        )
        if not data:
            return None

        item_id = (data.get("addProjectV2ItemById") or {}).get("item", {}).get("id")
        return item_id

    async def set_item_status(self, item_id: str, status_label: str) -> None:
        """Set the Status field on a project item."""
        if not self._status_field_id:
            return

        option_id = self._status_options.get(status_label)
        if not option_id:
            logger.debug(f"Unknown project status '{status_label}', skipping")
            return

        mutation = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId,
            itemId: $itemId,
            fieldId: $fieldId,
            value: {singleSelectOptionId: $optionId}
          }) {
            projectV2Item { id }
          }
        }
        """
        await self._graphql(
            mutation,
            {
                "projectId": self._project_id,
                "itemId": item_id,
                "fieldId": self._status_field_id,
                "optionId": option_id,
            },
        )

    async def sync_status(self, node_id: str | None, label: str) -> None:
        """Add item to project (if needed) and set its status.

        Called from LabelManager.transition_to(). Safe to call even if
        Projects integration is disabled — it's a no-op.
        """
        if not self.is_configured() or not node_id:
            return

        status_name = self._label_status_map.get(label)
        if not status_name:
            return

        item_id = await self.add_item_to_project(node_id)
        if item_id:
            await self.set_item_status(item_id, status_name)

    async def close(self) -> None:
        await self._client.aclose()


_project_manager: ProjectManager | None = None


def get_project_manager() -> ProjectManager:
    global _project_manager
    if _project_manager is None:
        _project_manager = ProjectManager()
    return _project_manager
