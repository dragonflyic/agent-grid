"""Dashboard API for pipeline visibility and manual controls."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("agent_grid.dashboard")

dashboard_router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ActivateRequest(BaseModel):
    issue_numbers: list[int]


class ClassifyRequest(BaseModel):
    issue_numbers: list[int]


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@dashboard_router.get("/overview")
async def pipeline_overview(repo: str | None = None) -> dict:
    """Pipeline funnel: total open issues, labeled/unlabeled, classifications, budget."""
    from ..config import settings
    from ..issue_tracker import get_issue_tracker
    from ..issue_tracker.public_api import IssueStatus
    from .budget_manager import get_budget_manager
    from .database import get_database

    actual_repo = repo or settings.target_repo
    if not actual_repo:
        raise HTTPException(status_code=400, detail="No target_repo configured")

    db = get_database()
    tracker = get_issue_tracker()

    all_open = await tracker.list_issues(actual_repo, status=IssueStatus.OPEN)

    issues_by_label: dict[str, int] = {}
    labeled_count = 0
    for issue in all_open:
        has_ag = False
        for label in issue.labels:
            if label.startswith("ag/"):
                has_ag = True
                issues_by_label[label] = issues_by_label.get(label, 0) + 1
        if has_ag:
            labeled_count += 1

    stats = await db.get_pipeline_stats(actual_repo)
    budget = await get_budget_manager().get_budget_status()

    return {
        "repo": actual_repo,
        "total_open_issues": len(all_open),
        "labeled_issues": labeled_count,
        "unlabeled_issues": len(all_open) - labeled_count,
        "issues_by_label": issues_by_label,
        "classifications": stats["classifications"],
        "execution_counts": stats["execution_counts"],
        "total_tracked_issues": stats["total_tracked_issues"],
        "budget": budget,
    }


@dashboard_router.get("/issues")
async def list_issues(
    repo: str | None = None,
    stage: str | None = None,
    classification: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """All open issues merged with DB state. Filterable by stage/classification."""
    from ..config import settings
    from ..issue_tracker import get_issue_tracker
    from ..issue_tracker.public_api import IssueStatus
    from .database import get_database

    actual_repo = repo or settings.target_repo
    if not actual_repo:
        raise HTTPException(status_code=400, detail="No target_repo configured")

    db = get_database()
    tracker = get_issue_tracker()

    all_open = await tracker.list_issues(actual_repo, status=IssueStatus.OPEN)
    all_states = await db.list_all_issue_states(actual_repo, limit=9999)
    state_map = {s["issue_number"]: s for s in all_states}

    results = []
    for issue in all_open:
        state = state_map.get(issue.number, {})
        issue_class = state.get("classification")

        if classification and issue_class != classification:
            continue

        ag_labels = [la for la in issue.labels if la.startswith("ag/")]
        pipeline_stage = _derive_stage(ag_labels)

        if stage and pipeline_stage != stage:
            continue

        metadata = state.get("metadata") or {}
        results.append(
            {
                "issue_number": issue.number,
                "title": issue.title,
                "author": issue.author,
                "labels": issue.labels,
                "ag_labels": ag_labels,
                "pipeline_stage": pipeline_stage,
                "classification": issue_class,
                "confidence_score": metadata.get("confidence_score"),
                "confidence_verdict": metadata.get("confidence_verdict"),
                "retry_count": state.get("retry_count", 0),
                "last_checked_at": state.get("last_checked_at"),
                "created_at": issue.created_at.isoformat() if issue.created_at else None,
            }
        )

    results.sort(key=lambda x: x["issue_number"], reverse=True)
    return results[offset : offset + limit]


@dashboard_router.get("/issues/{issue_number}")
async def get_issue_detail(issue_number: int, repo: str | None = None) -> dict:
    """Full detail: GitHub info + DB state + audit trail + execution history."""
    from ..config import settings
    from ..issue_tracker import get_issue_tracker
    from .database import get_database

    actual_repo = repo or settings.target_repo
    if not actual_repo:
        raise HTTPException(status_code=400, detail="No target_repo configured")

    db = get_database()
    tracker = get_issue_tracker()

    state = await db.get_issue_state(issue_number, actual_repo)
    events = await db.get_pipeline_events(actual_repo, issue_number=issue_number, limit=50)
    executions = await db.list_executions_for_dashboard(issue_id=str(issue_number), limit=20)

    github_info = None
    try:
        gh_issue = await tracker.get_issue(actual_repo, str(issue_number))
        github_info = {
            "title": gh_issue.title,
            "labels": gh_issue.labels,
            "author": gh_issue.author,
            "status": gh_issue.status.value if gh_issue.status else None,
            "created_at": gh_issue.created_at.isoformat() if gh_issue.created_at else None,
        }
    except Exception:
        logger.warning(f"Failed to fetch GitHub info for issue #{issue_number}")

    return {
        "issue_number": issue_number,
        "repo": actual_repo,
        "github": github_info,
        "db_state": state,
        "pipeline_events": events,
        "executions": executions,
    }


@dashboard_router.get("/activity")
async def activity_feed(
    repo: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Recent pipeline events across all issues."""
    from ..config import settings
    from .database import get_database

    actual_repo = repo or settings.target_repo
    if not actual_repo:
        raise HTTPException(status_code=400, detail="No target_repo configured")

    db = get_database()
    return await db.get_pipeline_events(
        actual_repo,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )


@dashboard_router.get("/executions")
async def list_executions(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List executions across all issues. Filterable by status."""
    from .database import get_database

    db = get_database()
    return await db.list_all_executions_for_dashboard(status=status, limit=limit, offset=offset)


@dashboard_router.get("/executions/{execution_id}/events")
async def get_execution_events(
    execution_id: str,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    """Get agent chat/tool events for an execution."""
    from uuid import UUID as _UUID

    from .database import get_database

    db = get_database()
    try:
        exec_uuid = _UUID(execution_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid execution_id")

    return await db.get_agent_events(exec_uuid, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@dashboard_router.post("/actions/activate")
async def activate_issues(request: ActivateRequest, repo: str | None = None) -> dict:
    """Add ag/todo label to specified issues, entering them into the pipeline."""
    from ..config import settings
    from ..issue_tracker.label_manager import get_label_manager
    from .database import get_database

    actual_repo = repo or settings.target_repo
    if not actual_repo:
        raise HTTPException(status_code=400, detail="No target_repo configured")

    db = get_database()
    labels = get_label_manager()

    activated = []
    errors = []
    for num in request.issue_numbers:
        try:
            await labels.add_label(actual_repo, str(num), "ag/todo")
            await db.record_pipeline_event(
                issue_number=num,
                repo=actual_repo,
                event_type="manual_activate",
                stage="manual",
                detail={"action": "add_ag_todo"},
            )
            activated.append(num)
        except Exception as e:
            errors.append({"issue_number": num, "error": str(e)})

    return {"activated": activated, "errors": errors}


@dashboard_router.post("/actions/classify")
async def classify_issues(request: ClassifyRequest, repo: str | None = None) -> dict:
    """Run classifier on specified issues (no ag/* label needed)."""
    from ..config import settings
    from ..issue_tracker import get_issue_tracker
    from .classifier import get_classifier
    from .database import get_database

    actual_repo = repo or settings.target_repo
    if not actual_repo:
        raise HTTPException(status_code=400, detail="No target_repo configured")

    db = get_database()
    tracker = get_issue_tracker()
    classifier = get_classifier()

    results = []
    for num in request.issue_numbers:
        try:
            issue = await tracker.get_issue(actual_repo, str(num))
            classification = await classifier.classify(issue)
            await db.upsert_issue_state(
                issue_number=num,
                repo=actual_repo,
                classification=classification.category,
            )
            await db.record_pipeline_event(
                issue_number=num,
                repo=actual_repo,
                event_type="manual_classify",
                stage="manual",
                detail={
                    "category": classification.category,
                    "reason": classification.reason,
                    "estimated_complexity": classification.estimated_complexity,
                },
            )
            results.append(
                {
                    "issue_number": num,
                    "classification": classification.category,
                    "reason": classification.reason,
                }
            )
        except Exception as e:
            results.append({"issue_number": num, "error": str(e)})

    return {"results": results}


@dashboard_router.post("/actions/retry/{issue_number}")
async def retry_issue(issue_number: int, repo: str | None = None) -> dict:
    """Reset a failed/skipped issue back to ag/todo."""
    from ..config import settings
    from ..issue_tracker.label_manager import get_label_manager
    from .database import get_database

    actual_repo = repo or settings.target_repo
    if not actual_repo:
        raise HTTPException(status_code=400, detail="No target_repo configured")

    db = get_database()
    labels = get_label_manager()

    await labels.transition_to(actual_repo, str(issue_number), "ag/todo")
    await db.upsert_issue_state(
        issue_number=issue_number,
        repo=actual_repo,
        retry_count=0,
    )
    await db.record_pipeline_event(
        issue_number=issue_number,
        repo=actual_repo,
        event_type="manual_retry",
        stage="manual",
        detail={"action": "reset_to_todo"},
    )
    return {"status": "retried", "issue_number": issue_number}


@dashboard_router.post("/actions/scan")
async def trigger_scan(repo: str | None = None) -> dict:
    """Trigger an immediate management loop cycle in the background."""
    from ..config import settings
    from .management_loop import get_management_loop

    actual_repo = repo or settings.target_repo
    if not actual_repo:
        raise HTTPException(status_code=400, detail="No target_repo configured")

    loop = get_management_loop()
    asyncio.create_task(loop.run_cycle())
    return {"status": "scan_triggered", "repo": actual_repo}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STAGE_PRIORITY = [
    ("ag/in-progress", "in-progress"),
    ("ag/planning", "planning"),
    ("ag/review-pending", "review-pending"),
    ("ag/blocked", "blocked"),
    ("ag/waiting", "waiting"),
    ("ag/done", "done"),
    ("ag/failed", "failed"),
    ("ag/skipped", "skipped"),
    ("ag/epic", "epic"),
    ("ag/todo", "todo"),
    ("ag/sub-issue", "sub-issue"),
    ("ag/proactive", "proactive"),
]


def _derive_stage(ag_labels: list[str]) -> str:
    """Derive a single pipeline stage from ag/* labels."""
    if not ag_labels:
        return "unlabeled"
    for label, stage in _STAGE_PRIORITY:
        if label in ag_labels:
            return stage
    return "labeled-other"
