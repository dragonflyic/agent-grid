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
from .issue_tracker.public_api import (
    IssueInfo,
    IssueStatus,
    IssueTracker,
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


class DryRunIssueTracker(IssueTracker):
    """Wraps a real IssueTracker. Reads pass through; writes are logged.

    Uses the IssueTracker ABC directly — no isinstance checks needed.
    """

    def __init__(self, real: IssueTracker):
        self._real = real
        self._log = get_dry_logger()
        self._fake_issue_counter = 90000

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

    async def list_open_prs(self, repo: str, **params) -> list[dict]:
        return await self._real.list_open_prs(repo, **params)

    async def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict]:
        return await self._real.get_pr_reviews(repo, pr_number)

    async def get_pr_comments(self, repo: str, pr_number: int) -> list[dict]:
        return await self._real.get_pr_comments(repo, pr_number)

    async def get_pr_by_branch(self, repo: str, branch: str) -> dict | None:
        return await self._real.get_pr_by_branch(repo, branch)

    async def get_pr_data(self, repo: str, pr_number: int) -> dict | None:
        return await self._real.get_pr_data(repo, pr_number)

    async def get_issue_comments_since(self, repo: str, issue_id: str, since: str | None = None) -> list[dict]:
        return await self._real.get_issue_comments_since(repo, issue_id, since=since)

    async def get_check_runs_for_ref(self, repo: str, ref: str, *, status: str = "completed") -> list[dict]:
        return await self._real.get_check_runs_for_ref(repo, ref, status=status)

    async def get_actions_job_logs(self, repo: str, job_id: int) -> str:
        return await self._real.get_actions_job_logs(repo, job_id)

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
        self._log.log("add_comment", repo=repo, issue_id=issue_id, body=body[:500])

    async def update_issue_status(self, repo: str, issue_id: str, status: IssueStatus) -> None:
        self._log.log("update_issue_status", repo=repo, issue_id=issue_id, status=status.value)

    async def add_label(self, repo: str, issue_id: str, label: str) -> None:
        self._log.log("add_label", repo=repo, issue_id=issue_id, label=label)

    async def remove_label(self, repo: str, issue_id: str, label: str) -> None:
        self._log.log("remove_label", repo=repo, issue_id=issue_id, label=label)

    async def create_label(self, repo: str, name: str, color: str) -> bool:
        self._log.log("create_label", repo=repo, name=name, color=color)
        return True

    async def assign_issue(self, repo: str, issue_id: str, assignee: str) -> None:
        self._log.log("assign_issue", repo=repo, issue_id=issue_id, assignee=assignee)

    async def request_pr_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
        self._log.log("request_pr_reviewers", repo=repo, pr_number=pr_number, reviewers=reviewers)

    async def add_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        self._log.log("add_pr_comment", repo=repo, pr_number=pr_number, body=body[:500])

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
        self._pipeline_events: list[dict] = []
        self._agent_events: list[dict] = []

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def create_execution(self, execution: AgentExecution, issue_id: str) -> None:
        self._executions[execution.id] = {"execution": execution, "issue_id": issue_id}

    async def try_claim_issue(self, execution: AgentExecution, issue_id: str) -> bool:
        for e in self._executions.values():
            if e["issue_id"] == issue_id and e["execution"].status in (
                ExecutionStatus.PENDING,
                ExecutionStatus.RUNNING,
            ):
                return False
        self._executions[execution.id] = {"execution": execution, "issue_id": issue_id}
        return True

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

    async def merge_issue_metadata(self, issue_number: int, repo: str, metadata_update: dict) -> None:
        key = (issue_number, repo)
        if key not in self._issue_states:
            self._issue_states[key] = {"issue_number": issue_number, "repo": repo, "retry_count": 0, "metadata": {}}
        existing = self._issue_states[key].get("metadata") or {}
        self._issue_states[key]["metadata"] = {**existing, **metadata_update}

    async def get_cron_state(self, key: str) -> dict | None:
        return self._cron_state.get(key)

    async def set_cron_state(self, key: str, value: dict) -> None:
        self._cron_state[key] = value

    async def save_checkpoint(self, execution_id: UUID, checkpoint: dict) -> None:
        self._checkpoints[str(execution_id)] = checkpoint

    async def get_latest_checkpoint(self, issue_id: str) -> dict | None:
        return self._checkpoints.get(issue_id)

    async def get_all_checkpoints(self, issue_id: str) -> list[dict]:
        return []

    async def get_pending_nudges(self, limit: int = 10) -> list:
        return []

    async def create_nudge(self, nudge) -> None:
        pass

    async def mark_nudge_processed(self, nudge_id: UUID) -> None:
        pass

    async def list_issue_states(self, repo: str, classification: str | None = None) -> list[dict]:
        results = list(self._issue_states.values())
        if repo:
            results = [s for s in results if s.get("repo") == repo]
        if classification:
            results = [s for s in results if s.get("classification") == classification]
        return results

    async def update_execution_result(
        self,
        execution_id: UUID,
        status=None,
        result: str | None = None,
        pr_number: int | None = None,
        branch: str | None = None,
        checkpoint: dict | None = None,
    ) -> None:
        if execution_id in self._executions:
            execution = self._executions[execution_id]["execution"]
            if status is not None:
                execution.status = status
            if result is not None:
                execution.result = result

    async def set_external_run_id(self, execution_id: UUID, external_run_id: str) -> None:
        pass

    async def get_active_executions_with_external_run_id(self) -> list[tuple[UUID, str]]:
        return []

    async def record_budget_usage(self, **kwargs) -> None:
        pass

    async def get_total_budget_usage(self, **kwargs) -> dict:
        return {"tokens_used": 0, "duration_seconds": 0}

    async def count_oz_runs_today(self) -> int:
        return 0

    # Pipeline events (audit trail)

    async def record_pipeline_event(
        self, issue_number: int, repo: str, event_type: str, stage: str, detail: dict | None = None
    ) -> None:
        self._pipeline_events.append(
            {
                "id": str(uuid4()),
                "issue_number": issue_number,
                "repo": repo,
                "event_type": event_type,
                "stage": stage,
                "detail": detail,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def get_pipeline_events(
        self,
        repo: str,
        issue_number: int | None = None,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        events = self._pipeline_events
        events = [e for e in events if e["repo"] == repo]
        if issue_number is not None:
            events = [e for e in events if e["issue_number"] == issue_number]
        if event_type is not None:
            events = [e for e in events if e["event_type"] == event_type]
        events.sort(key=lambda e: e["created_at"], reverse=True)
        return events[offset : offset + limit]

    async def get_pipeline_stats(self, repo: str) -> dict:
        classifications: dict[str, int] = {}
        for state in self._issue_states.values():
            if state.get("repo") == repo:
                c = state.get("classification") or "unclassified"
                classifications[c] = classifications.get(c, 0) + 1
        execution_counts: dict[str, int] = {}
        for e in self._executions.values():
            # Filter by repo (repo_url is like "https://github.com/org/repo.git")
            if repo not in (e["execution"].repo_url or ""):
                continue
            s = e["execution"].status.value if hasattr(e["execution"].status, "value") else str(e["execution"].status)
            execution_counts[s] = execution_counts.get(s, 0) + 1
        total = sum(1 for s in self._issue_states.values() if s.get("repo") == repo)
        return {"classifications": classifications, "execution_counts": execution_counts, "total_tracked_issues": total}

    async def list_all_issue_states(self, repo: str, limit: int = 500, offset: int = 0) -> list[dict]:
        results = [s for s in self._issue_states.values() if s.get("repo") == repo]
        return results[offset : offset + limit]

    # Agent events (execution-level audit trail)

    async def record_agent_event(
        self,
        execution_id: UUID,
        message_type: str,
        content: str | None = None,
        tool_name: str | None = None,
        tool_id: str | None = None,
    ) -> None:
        self._agent_events.append(
            {
                "id": str(uuid4()),
                "execution_id": str(execution_id),
                "message_type": message_type,
                "content": content,
                "tool_name": tool_name,
                "tool_id": tool_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def get_agent_events(
        self,
        execution_id: UUID,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        events = self._agent_events
        filtered = [e for e in events if e["execution_id"] == str(execution_id)]
        filtered.sort(key=lambda e: e["created_at"])
        return filtered[offset : offset + limit]

    async def list_executions_for_dashboard(
        self,
        issue_id: str,
        limit: int = 20,
    ) -> list[dict]:
        results = []
        for entry in self._executions.values():
            if entry["issue_id"] == issue_id:
                e = entry["execution"]
                s = e.status.value if hasattr(e.status, "value") else e.status
                results.append(
                    {
                        "id": str(e.id),
                        "status": s,
                        "mode": e.mode,
                        "prompt": e.prompt,
                        "result": e.result,
                        "pr_number": None,
                        "branch": None,
                        "external_run_id": None,
                        "session_link": None,
                        "cost_cents": None,
                        "started_at": e.started_at.isoformat() if e.started_at else None,
                        "completed_at": e.completed_at.isoformat() if e.completed_at else None,
                        "created_at": e.created_at.isoformat() if hasattr(e, "created_at") and e.created_at else None,
                    }
                )
        return results[:limit]

    async def list_all_executions_for_dashboard(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        results = []
        for entry in self._executions.values():
            e = entry["execution"]
            s = e.status.value if hasattr(e.status, "value") else e.status
            if status and s != status:
                continue
            results.append(
                {
                    "id": str(e.id),
                    "issue_id": entry["issue_id"],
                    "status": s,
                    "mode": e.mode,
                    "prompt": (e.prompt or "")[:200],
                    "result": (e.result or "")[:200],
                    "pr_number": None,
                    "branch": None,
                    "external_run_id": None,
                    "session_link": None,
                    "cost_cents": None,
                    "started_at": e.started_at.isoformat() if e.started_at else None,
                    "completed_at": e.completed_at.isoformat() if e.completed_at else None,
                    "created_at": e.created_at.isoformat() if hasattr(e, "created_at") and e.created_at else None,
                }
            )
        return results[offset : offset + limit]

    async def get_execution_counts_by_issue(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._executions.values():
            iid = entry["issue_id"]
            counts[iid] = counts.get(iid, 0) + 1
        return counts

    async def set_session_link(self, execution_id: UUID, session_link: str) -> None:
        pass

    async def set_cost(self, execution_id: UUID, cost_cents: int) -> None:
        pass


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
        execution_id: UUID | None = None,
    ) -> UUID:
        execution_id = execution_id or uuid4()
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
    # Reset Oz grid singleton too
    import agent_grid.execution_grid.oz_grid as oz_mod

    oz_mod._oz_grid = None
    # Also override mode so get_execution_grid returns our dry grid
    settings.deployment_mode = "coordinator"

    # Reset service singletons that cache references to the real tracker/labels
    # so they pick up the dry-run wrappers when re-initialized
    import agent_grid.coordinator.agent_launcher as launcher_mod
    import agent_grid.coordinator.blocker_resolver as blocker_mod
    import agent_grid.coordinator.budget_manager as budget_mod
    import agent_grid.coordinator.ci_monitor as ci_monitor_mod
    import agent_grid.coordinator.dependency_resolver as dep_mod
    import agent_grid.coordinator.planner as planner_mod
    import agent_grid.coordinator.pr_monitor as pr_monitor_mod
    import agent_grid.coordinator.proactive_scanner as proactive_scanner_mod
    import agent_grid.coordinator.quality_gate as quality_gate_mod
    import agent_grid.coordinator.scanner as scanner_mod
    import agent_grid.issue_tracker.project_manager as project_mod

    launcher_mod._agent_launcher = None
    ci_monitor_mod._ci_monitor = None
    project_mod._project_manager = None
    settings.github_project_number = None  # Disable Projects in dry-run
    scanner_mod._scanner = None
    planner_mod._planner = None
    pr_monitor_mod._pr_monitor = None
    blocker_mod._blocker_resolver = None
    dep_mod._resolver = None
    budget_mod._budget_manager = None
    quality_gate_mod._quality_gate = None
    proactive_scanner_mod._proactive_scanner = None

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
