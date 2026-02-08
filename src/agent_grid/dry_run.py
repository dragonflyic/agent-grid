"""Dry-run wrappers that intercept all write operations and log them to a file.

Read operations pass through to the real GitHub API. Write operations
(add_comment, create_subissue, update_issue_status, label changes, agent launches)
are logged to a JSONL file instead of being executed.

Usage:
    AGENT_GRID_DRY_RUN=true AGENT_GRID_TARGET_REPO=myorg/myrepo python -m agent_grid.dry_run
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .config import settings
from .execution_grid.public_api import (
    AgentEventHandler,
    AgentExecution,
    ExecutionConfig,
    ExecutionGrid,
    ExecutionStatus,
)
from .issue_tracker.github_client import GitHubClient
from .issue_tracker.public_api import (
    IssueInfo,
    IssueStatus,
)

logger = logging.getLogger("agent_grid.dry_run")


class DryRunLogger:
    """Writes intercepted actions to a JSONL file."""

    def __init__(self, output_file: str | None = None):
        self._path = Path(output_file or settings.dry_run_output_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Clear file at start of run
        self._path.write_text("")
        logger.info(f"Dry-run output → {self._path.resolve()}")

    def log(self, action: str, **kwargs) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **{k: _serialize(v) for k, v in kwargs.items()},
        }
        with self._path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        logger.info(f"[DRY RUN] {action}: {json.dumps({k: _serialize(v) for k, v in kwargs.items()}, default=str)}")


def _serialize(val):
    """Make values JSON-serializable."""
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, IssueInfo):
        return {"number": val.number, "title": val.title, "id": val.id}
    return val


# Global logger instance
_dry_logger: DryRunLogger | None = None


def get_dry_logger() -> DryRunLogger:
    global _dry_logger
    if _dry_logger is None:
        _dry_logger = DryRunLogger()
    return _dry_logger


class DryRunIssueTracker(GitHubClient):
    """Wraps a real GitHubClient. Reads pass through; writes are logged.

    Inherits from GitHubClient so isinstance() checks in PRMonitor,
    BlockerResolver, and Planner pass naturally. The real GitHubClient's
    _client (httpx) is exposed for direct read-only API calls.
    """

    def __init__(self, real: GitHubClient):
        # Skip GitHubClient.__init__ — we delegate to the real instance
        self._real = real
        self._log = get_dry_logger()
        self._fake_issue_counter = 90000
        # Expose _client for PRMonitor and other code that reads via _client directly
        self._client = real._client
        self._token = real._token

    # ---- READS (pass through) ----

    async def get_issue(self, repo: str, issue_id: str) -> IssueInfo:
        return await self._real.get_issue(repo, issue_id)

    async def list_subissues(self, repo: str, parent_id: str) -> list[IssueInfo]:
        return await self._real.list_subissues(repo, parent_id)

    async def list_issues(
        self,
        repo: str,
        status: IssueStatus | None = None,
        labels: list[str] | None = None,
    ) -> list[IssueInfo]:
        return await self._real.list_issues(repo, status=status, labels=labels)

    # ---- WRITES (intercepted) ----

    async def create_subissue(
        self,
        repo: str,
        parent_id: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> IssueInfo:
        self._fake_issue_counter += 1
        fake_number = self._fake_issue_counter
        self._log.log(
            "create_subissue",
            repo=repo,
            parent_id=parent_id,
            title=title,
            body=body[:500],
            labels=labels,
            fake_number=fake_number,
        )
        # Return a fake IssueInfo so the rest of the pipeline can continue
        return IssueInfo(
            id=str(fake_number),
            number=fake_number,
            title=title,
            body=body,
            status=IssueStatus.OPEN,
            labels=labels or [],
            repo_url=f"https://github.com/{repo}",
            html_url=f"https://github.com/{repo}/issues/{fake_number}",
            parent_id=parent_id,
        )

    async def add_comment(self, repo: str, issue_id: str, body: str) -> None:
        self._log.log(
            "add_comment",
            repo=repo,
            issue_id=issue_id,
            body=body[:500],
        )

    async def update_issue_status(self, repo: str, issue_id: str, status: IssueStatus) -> None:
        self._log.log(
            "update_issue_status",
            repo=repo,
            issue_id=issue_id,
            status=status.value,
        )

    async def _add_label(self, repo: str, issue_id: str, label: str) -> None:
        self._log.log("_add_label", repo=repo, issue_id=issue_id, label=label)

    async def _remove_label(self, repo: str, issue_id: str, label: str) -> None:
        self._log.log("_remove_label", repo=repo, issue_id=issue_id, label=label)

    async def close(self) -> None:
        await self._real.close()


class DryRunDatabase:
    """In-memory database stub for dry-run mode. No PostgreSQL needed."""

    def __init__(self):
        self._pool = None  # Signals "not connected" to main.py checks
        self._executions: dict[UUID, dict] = {}
        self._issue_states: dict[tuple[int, str], dict] = {}
        self._cron_state: dict[str, dict] = {}
        self._checkpoints: dict[str, dict] = {}

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def create_execution(self, execution: AgentExecution, issue_id: str) -> None:
        self._executions[execution.id] = {"execution": execution, "issue_id": issue_id}

    async def update_execution(self, execution: AgentExecution) -> None:
        if execution.id in self._executions:
            self._executions[execution.id]["execution"] = execution

    async def get_execution(self, execution_id: UUID) -> AgentExecution | None:
        entry = self._executions.get(execution_id)
        return entry["execution"] if entry else None

    async def list_executions(self, status=None, **kwargs) -> list[AgentExecution]:
        results = [e["execution"] for e in self._executions.values()]
        if status:
            results = [e for e in results if e.status == status]
        return results

    async def get_running_executions(self) -> list[AgentExecution]:
        return [e["execution"] for e in self._executions.values() if e["execution"].status == ExecutionStatus.RUNNING]

    async def get_execution_for_issue(self, issue_id: str) -> AgentExecution | None:
        for e in self._executions.values():
            if e["issue_id"] == issue_id:
                return e["execution"]
        return None

    async def get_issue_id_for_execution(self, execution_id: UUID) -> str | None:
        entry = self._executions.get(execution_id)
        return entry["issue_id"] if entry else None

    async def upsert_issue_state(self, issue_number: int, repo: str, **kwargs) -> None:
        key = (issue_number, repo)
        if key not in self._issue_states:
            self._issue_states[key] = {"issue_number": issue_number, "repo": repo, "retry_count": 0}
        self._issue_states[key].update({k: v for k, v in kwargs.items() if v is not None})

    async def get_issue_state(self, issue_number: int, repo: str) -> dict | None:
        return self._issue_states.get((issue_number, repo))

    async def get_cron_state(self, key: str) -> dict | None:
        return self._cron_state.get(key)

    async def set_cron_state(self, key: str, value: dict) -> None:
        self._cron_state[key] = value

    async def save_checkpoint(self, issue_id: str, checkpoint: dict) -> None:
        self._checkpoints[issue_id] = checkpoint

    async def get_latest_checkpoint(self, issue_id: str) -> dict | None:
        return self._checkpoints.get(issue_id)

    async def get_pending_nudges(self, limit: int = 10) -> list:
        return []

    async def record_budget_usage(self, **kwargs) -> None:
        pass

    async def get_total_budget_usage(self, **kwargs) -> dict:
        return {"tokens_used": 0, "duration_seconds": 0}


class DryRunLabelManager:
    """Intercepts label changes and logs them instead of applying."""

    def __init__(self):
        self._log = get_dry_logger()

    async def transition_to(self, repo: str, issue_id: str, new_label: str) -> None:
        self._log.log("label_transition", repo=repo, issue_id=issue_id, new_label=new_label)

    async def add_label(self, repo: str, issue_id: str, label: str) -> None:
        self._log.log("add_label", repo=repo, issue_id=issue_id, label=label)

    async def remove_label(self, repo: str, issue_id: str, label: str) -> None:
        self._log.log("remove_label", repo=repo, issue_id=issue_id, label=label)

    async def ensure_labels_exist(self, repo: str) -> None:
        self._log.log("ensure_labels_exist", repo=repo)


class DryRunExecutionGrid(ExecutionGrid):
    """Logs agent launches instead of actually spawning machines."""

    def __init__(self):
        self._log = get_dry_logger()
        self._executions: dict[UUID, AgentExecution] = {}

    async def launch_agent(
        self,
        config: ExecutionConfig,
        mode: str = "implement",
        issue_number: int | None = None,
        context: dict | None = None,
    ) -> UUID:
        execution_id = uuid4()
        self._log.log(
            "launch_agent",
            execution_id=execution_id,
            repo_url=config.repo_url,
            mode=mode,
            issue_number=issue_number,
            prompt_preview=config.prompt[:300],
        )
        self._executions[execution_id] = AgentExecution(
            id=execution_id,
            repo_url=config.repo_url,
            status=ExecutionStatus.PENDING,
            prompt=config.prompt,
        )
        return execution_id

    async def get_execution_status(self, execution_id: UUID) -> AgentExecution | None:
        return self._executions.get(execution_id)

    def get_active_executions(self) -> list[AgentExecution]:
        return list(self._executions.values())

    async def cancel_execution(self, execution_id: UUID) -> bool:
        return self._executions.pop(execution_id, None) is not None

    def subscribe_to_agent_events(self, handler: AgentEventHandler) -> None:
        pass

    def unsubscribe_from_agent_events(self, handler: AgentEventHandler) -> None:
        pass


def install_dry_run_wrappers() -> None:
    """Replace global singletons with dry-run wrappers.

    Call this before any services are initialized.
    """
    import agent_grid.coordinator.database as db_mod
    import agent_grid.execution_grid.service as grid_service
    import agent_grid.issue_tracker.label_manager as label_mod
    import agent_grid.issue_tracker.public_api as tracker_api

    # Force dry_run on
    settings.dry_run = True

    # Replace database with in-memory stub (no PostgreSQL needed)
    db_mod._database = DryRunDatabase()

    # Ensure we're using the GitHub issue tracker for reads
    if settings.issue_tracker_type != "github":
        settings.issue_tracker_type = "github"

    # Wrap the issue tracker — reads go to real GitHub, writes are logged
    real_tracker = tracker_api.get_issue_tracker()
    dry_tracker = DryRunIssueTracker(real_tracker)
    tracker_api._issue_tracker = dry_tracker

    # Replace label manager
    label_mod._label_manager = DryRunLabelManager()

    # Replace execution grid
    grid_service._service = None
    grid_service._fly_grid = DryRunExecutionGrid()
    # Also override mode so get_execution_grid returns our dry grid
    settings.deployment_mode = "coordinator"

    # Reset service singletons that cache references to the real tracker/labels
    # so they pick up the dry-run wrappers when re-initialized
    import agent_grid.coordinator.blocker_resolver as blocker_mod
    import agent_grid.coordinator.budget_manager as budget_mod
    import agent_grid.coordinator.dependency_resolver as dep_mod
    import agent_grid.coordinator.planner as planner_mod
    import agent_grid.coordinator.pr_monitor as pr_monitor_mod
    import agent_grid.coordinator.scanner as scanner_mod

    scanner_mod._scanner = None
    planner_mod._planner = None
    pr_monitor_mod._pr_monitor = None
    blocker_mod._blocker_resolver = None
    dep_mod._resolver = None
    budget_mod._budget_manager = None

    logger.info("Dry-run wrappers installed — writes will be logged, not executed")


async def run_single_cycle() -> str:
    """Run a single management loop cycle in dry-run mode.

    Returns the path to the output JSONL file.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    install_dry_run_wrappers()

    # Run one cycle
    from .coordinator.management_loop import ManagementLoop

    loop = ManagementLoop()
    await loop.run_once()

    # Cleanup
    import agent_grid.issue_tracker.public_api as tracker_api

    if tracker_api._issue_tracker:
        await tracker_api._issue_tracker.close()

    output_path = str(Path(settings.dry_run_output_file).resolve())
    logger.info(f"\nDry run complete! Review output at: {output_path}")
    return output_path


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_single_cycle())
