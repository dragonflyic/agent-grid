"""Fly Machines API client for spawning ephemeral worker containers."""

import logging
import time

import httpx

from ..config import settings

logger = logging.getLogger("agent_grid.fly")

FLY_API_BASE = "https://api.machines.dev/v1"


class FlyMachinesClient:
    """Client for the Fly Machines REST API.

    Spawns ephemeral Fly Machines that:
    1. Boot with the worker Docker image
    2. Clone the target repo
    3. Run Claude Code SDK against an issue
    4. POST results back to the orchestrator
    5. Self-destruct (auto_destroy=True)
    """

    def __init__(
        self,
        api_token: str | None = None,
        app_name: str | None = None,
    ):
        self._api_token = api_token or settings.fly_api_token
        self._app_name = app_name or settings.fly_app_name
        self._client = httpx.AsyncClient(
            base_url=FLY_API_BASE,
            headers={"Authorization": f"Bearer {self._api_token}"},
            timeout=60.0,
        )

    async def spawn_worker(
        self,
        execution_id: str,
        repo_url: str,
        issue_number: int,
        prompt: str,
        mode: str = "implement",
        context_json: str = "{}",
    ) -> dict:
        """Spawn an ephemeral Fly Machine for a worker agent."""
        machine_name = f"worker-{issue_number}-{int(time.time())}"

        machine_config = {
            "name": machine_name,
            "config": {
                "image": settings.fly_worker_image,
                "env": {
                    "EXECUTION_ID": execution_id,
                    "REPO_URL": repo_url,
                    "ISSUE_NUMBER": str(issue_number),
                    "MODE": mode,
                    "PROMPT": prompt,
                    "CONTEXT_JSON": context_json,
                    "ANTHROPIC_API_KEY": settings.anthropic_api_key,
                    "GITHUB_TOKEN": settings.github_token,
                    "ORCHESTRATOR_URL": f"https://{self._app_name}.fly.dev",
                    "AGENT_BYPASS_PERMISSIONS": "true",
                },
                "guest": {
                    "cpu_kind": "shared",
                    "cpus": settings.fly_worker_cpus,
                    "memory_mb": settings.fly_worker_memory_mb,
                },
                "auto_destroy": True,
                "restart": {"policy": "no"},
            },
            "region": settings.fly_worker_region,
        }

        response = await self._client.post(
            f"/apps/{self._app_name}/machines",
            json=machine_config,
        )
        response.raise_for_status()

        machine = response.json()
        logger.info(
            f"Spawned Fly Machine {machine['id']} for issue #{issue_number} (execution={execution_id}, mode={mode})"
        )
        return machine

    async def get_machine_status(self, machine_id: str) -> dict:
        """Get the status of a Fly Machine."""
        response = await self._client.get(
            f"/apps/{self._app_name}/machines/{machine_id}",
        )
        response.raise_for_status()
        return response.json()

    async def destroy_machine(self, machine_id: str) -> None:
        """Force destroy a Fly Machine."""
        try:
            await self._client.delete(
                f"/apps/{self._app_name}/machines/{machine_id}",
                params={"force": "true"},
            )
            logger.info(f"Destroyed Fly Machine {machine_id}")
        except httpx.HTTPStatusError as e:
            logger.warning(f"Failed to destroy machine {machine_id}: {e}")

    async def list_machines(self) -> list[dict]:
        """List all machines in the app."""
        response = await self._client.get(f"/apps/{self._app_name}/machines")
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


_fly_client: FlyMachinesClient | None = None


def get_fly_client() -> FlyMachinesClient:
    """Get the global Fly Machines client instance."""
    global _fly_client
    if _fly_client is None:
        _fly_client = FlyMachinesClient()
    return _fly_client
