"""PostgreSQL database access for coordinator using SQLAlchemy 2.0 async ORM."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings
from ..execution_grid import AgentExecution, ExecutionStatus
from .models import (
    AgentEventModel,
    BudgetUsageModel,
    CronStateModel,
    ExecutionModel,
    IssueStateModel,
    NudgeModel,
    PipelineEventModel,
)
from .public_api import NudgeRequest


class Database:
    """PostgreSQL database interface using SQLAlchemy 2.0 async ORM."""

    def __init__(self, database_url: str | None = None):
        url = database_url or settings.database_url
        # Ensure async driver prefix for SQLAlchemy
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        self._engine = create_async_engine(url, pool_size=10, max_overflow=0)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def connect(self) -> None:
        """Verify database connectivity.

        Note: Schema is managed by Alembic migrations. Run migrations before
        starting the application:
            alembic upgrade head
        """
        async with self._engine.begin() as conn:
            await conn.execute(text("SELECT 1"))

    async def close(self) -> None:
        """Dispose of the engine and connection pool."""
        await self._engine.dispose()

    def _session(self) -> AsyncSession:
        """Create a new async session."""
        return self._session_factory()

    # -------------------------------------------------------------------------
    # Conversion helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _model_to_execution(m: ExecutionModel) -> AgentExecution:
        return AgentExecution(
            id=m.id,
            repo_url=m.repo_url,
            status=ExecutionStatus(m.status),
            prompt=m.prompt,
            result=m.result,
            mode=m.mode,
            started_at=m.started_at,
            completed_at=m.completed_at,
            created_at=m.created_at,
        )

    @staticmethod
    def _model_to_nudge(m: NudgeModel) -> NudgeRequest:
        return NudgeRequest(
            id=m.id,
            issue_id=m.issue_id,
            source_execution_id=m.source_execution_id,
            priority=m.priority,
            created_at=m.created_at,
            processed_at=m.processed_at,
        )

    # -------------------------------------------------------------------------
    # Execution operations
    # -------------------------------------------------------------------------

    async def create_execution(self, execution: AgentExecution, issue_id: str) -> None:
        """Insert a new execution record."""
        async with self._session() as session:
            session.add(
                ExecutionModel(
                    id=execution.id,
                    issue_id=issue_id,
                    repo_url=execution.repo_url,
                    status=execution.status.value,
                    prompt=execution.prompt,
                    result=execution.result,
                    mode=execution.mode,
                    started_at=execution.started_at,
                    completed_at=execution.completed_at,
                    created_at=execution.created_at,
                )
            )
            await session.commit()

    async def try_claim_issue(self, execution: AgentExecution, issue_id: str) -> bool:
        """Atomically claim an issue for execution, preventing duplicates.

        Returns True if the claim succeeded (no other active execution exists).
        Returns False if another pending/running execution already exists.

        Uses both WHERE NOT EXISTS check and the partial unique index
        (idx_executions_active_issue) as a safety net for concurrent claims.
        """
        async with self._session() as session:
            try:
                result = await session.execute(
                    text("""
                        INSERT INTO executions
                            (id, issue_id, repo_url, status, prompt, result, mode,
                             started_at, completed_at, created_at)
                        SELECT :id, :issue_id, :repo_url, :status, :prompt, :result, :mode,
                               :started_at, :completed_at, :created_at
                        WHERE NOT EXISTS (
                            SELECT 1 FROM executions
                            WHERE issue_id = :issue_id AND status IN ('pending', 'running')
                        )
                        RETURNING id
                    """),
                    {
                        "id": execution.id,
                        "issue_id": issue_id,
                        "repo_url": execution.repo_url,
                        "status": execution.status.value,
                        "prompt": execution.prompt,
                        "result": execution.result,
                        "mode": execution.mode,
                        "started_at": execution.started_at,
                        "completed_at": execution.completed_at,
                        "created_at": execution.created_at,
                    },
                )
                row = result.fetchone()
                await session.commit()
                return row is not None
            except IntegrityError:
                await session.rollback()
                return False

    async def update_execution(self, execution: AgentExecution) -> None:
        """Update an existing execution record."""
        async with self._session() as session:
            await session.execute(
                update(ExecutionModel)
                .where(ExecutionModel.id == execution.id)
                .values(
                    status=execution.status.value,
                    prompt=execution.prompt,
                    result=execution.result,
                    started_at=execution.started_at,
                    completed_at=execution.completed_at,
                )
            )
            await session.commit()

    async def get_execution(self, execution_id: UUID) -> AgentExecution | None:
        """Get an execution by ID."""
        async with self._session() as session:
            result = await session.execute(select(ExecutionModel).where(ExecutionModel.id == execution_id))
            m = result.scalar_one_or_none()
            return self._model_to_execution(m) if m else None

    async def list_executions(
        self,
        status: ExecutionStatus | None = None,
        issue_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentExecution]:
        """List executions with optional filters."""
        async with self._session() as session:
            stmt = select(ExecutionModel)
            if status:
                stmt = stmt.where(ExecutionModel.status == status.value)
            if issue_id:
                stmt = stmt.where(ExecutionModel.issue_id == issue_id)
            stmt = stmt.order_by(ExecutionModel.created_at.desc()).limit(limit).offset(offset)
            result = await session.execute(stmt)
            return [self._model_to_execution(m) for m in result.scalars().all()]

    async def get_running_executions(self) -> list[AgentExecution]:
        """Get all currently running executions."""
        return await self.list_executions(status=ExecutionStatus.RUNNING)

    async def get_execution_for_issue(self, issue_id: str) -> AgentExecution | None:
        """Get the most recent execution for an issue."""
        async with self._session() as session:
            result = await session.execute(
                select(ExecutionModel)
                .where(ExecutionModel.issue_id == issue_id)
                .order_by(ExecutionModel.created_at.desc())
                .limit(1)
            )
            m = result.scalar_one_or_none()
            return self._model_to_execution(m) if m else None

    async def get_issue_id_for_execution(self, execution_id: UUID) -> str | None:
        """Get the issue_id associated with an execution."""
        async with self._session() as session:
            result = await session.execute(select(ExecutionModel.issue_id).where(ExecutionModel.id == execution_id))
            row = result.scalar_one_or_none()
            return row

    # -------------------------------------------------------------------------
    # Nudge queue operations
    # -------------------------------------------------------------------------

    async def create_nudge(self, nudge: NudgeRequest) -> None:
        """Insert a new nudge request."""
        async with self._session() as session:
            session.add(
                NudgeModel(
                    id=nudge.id,
                    issue_id=nudge.issue_id,
                    source_execution_id=nudge.source_execution_id,
                    priority=nudge.priority,
                    created_at=nudge.created_at,
                    processed_at=nudge.processed_at,
                )
            )
            await session.commit()

    async def get_pending_nudges(self, limit: int = 10) -> list[NudgeRequest]:
        """Get pending nudge requests ordered by priority."""
        async with self._session() as session:
            result = await session.execute(
                select(NudgeModel)
                .where(NudgeModel.processed_at.is_(None))
                .order_by(NudgeModel.priority.desc(), NudgeModel.created_at.asc())
                .limit(limit)
            )
            return [self._model_to_nudge(m) for m in result.scalars().all()]

    async def mark_nudge_processed(self, nudge_id: UUID) -> None:
        """Mark a nudge request as processed."""
        async with self._session() as session:
            await session.execute(update(NudgeModel).where(NudgeModel.id == nudge_id).values(processed_at=func.now()))
            await session.commit()

    # -------------------------------------------------------------------------
    # Budget tracking operations
    # -------------------------------------------------------------------------

    async def record_budget_usage(
        self,
        execution_id: UUID,
        tokens_used: int,
        duration_seconds: int,
    ) -> None:
        """Record budget usage for an execution."""
        async with self._session() as session:
            # Let server_default handle id (gen_random_uuid()) and recorded_at (NOW())
            await session.execute(
                pg_insert(BudgetUsageModel).values(
                    execution_id=execution_id,
                    tokens_used=tokens_used,
                    duration_seconds=duration_seconds,
                )
            )
            await session.commit()

    async def get_total_budget_usage(
        self,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """Get total budget usage."""
        async with self._session() as session:
            stmt = select(
                func.coalesce(func.sum(BudgetUsageModel.tokens_used), 0).label("tokens"),
                func.coalesce(func.sum(BudgetUsageModel.duration_seconds), 0).label("duration"),
            )
            if since:
                stmt = stmt.where(BudgetUsageModel.recorded_at >= since)
            result = await session.execute(stmt)
            row = result.one()
            return {
                "tokens_used": row.tokens,
                "duration_seconds": row.duration,
            }

    async def count_oz_runs_today(self) -> int:
        """Count executions launched via Oz (external_run_id set) since midnight UTC."""
        async with self._session() as session:
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            result = await session.execute(
                select(func.count())
                .select_from(ExecutionModel)
                .where(
                    ExecutionModel.external_run_id.isnot(None),
                    ExecutionModel.created_at >= today_start,
                )
            )
            return result.scalar_one()

    # -------------------------------------------------------------------------
    # Issue state operations
    # -------------------------------------------------------------------------

    async def upsert_issue_state(
        self,
        issue_number: int,
        repo: str,
        classification: str | None = None,
        parent_issue: int | None = None,
        sub_issues: list[int] | None = None,
        retry_count: int = 0,
        metadata: dict | None = None,
    ) -> None:
        """Upsert an issue state record."""
        async with self._session() as session:
            # Use the column object directly for 'metadata' to avoid name conflicts
            metadata_col = IssueStateModel.__table__.c.metadata
            stmt = pg_insert(IssueStateModel).values(
                issue_number=issue_number,
                repo=repo,
                classification=classification,
                parent_issue=parent_issue,
                sub_issues=sub_issues,
                retry_count=retry_count,
                metadata_=metadata,  # SQLAlchemy JSON type handles serialization
                last_checked_at=func.now(),
                updated_at=func.now(),
            )
            excluded_metadata = stmt.excluded[metadata_col.name]
            stmt = stmt.on_conflict_do_update(
                index_elements=["issue_number", "repo"],
                set_={
                    "classification": func.coalesce(stmt.excluded.classification, IssueStateModel.classification),
                    "parent_issue": func.coalesce(stmt.excluded.parent_issue, IssueStateModel.parent_issue),
                    "sub_issues": func.coalesce(stmt.excluded.sub_issues, IssueStateModel.sub_issues),
                    "retry_count": stmt.excluded.retry_count,
                    metadata_col.name: func.coalesce(excluded_metadata, metadata_col),
                    "last_checked_at": func.now(),
                    "updated_at": func.now(),
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def get_issue_state(self, issue_number: int, repo: str) -> dict | None:
        """Get issue state by number and repo."""
        async with self._session() as session:
            result = await session.execute(
                select(IssueStateModel).where(
                    IssueStateModel.issue_number == issue_number, IssueStateModel.repo == repo
                )
            )
            m = result.scalar_one_or_none()
            if m is None:
                return None
            return {
                "issue_number": m.issue_number,
                "repo": m.repo,
                "classification": m.classification,
                "parent_issue": m.parent_issue,
                "sub_issues": m.sub_issues,
                "retry_count": m.retry_count,
                "metadata": m.metadata_,
                "last_checked_at": m.last_checked_at,
                "created_at": m.created_at,
                "updated_at": m.updated_at,
            }

    async def list_issue_states(self, repo: str, classification: str | None = None) -> list[dict]:
        """List issue states with optional classification filter."""
        async with self._session() as session:
            stmt = select(IssueStateModel).where(IssueStateModel.repo == repo)
            if classification:
                stmt = stmt.where(IssueStateModel.classification == classification)
            result = await session.execute(stmt)
            return [
                {
                    "issue_number": m.issue_number,
                    "repo": m.repo,
                    "classification": m.classification,
                    "parent_issue": m.parent_issue,
                    "sub_issues": m.sub_issues,
                    "retry_count": m.retry_count,
                    "metadata": m.metadata_,
                    "last_checked_at": m.last_checked_at,
                    "created_at": m.created_at,
                    "updated_at": m.updated_at,
                }
                for m in result.scalars().all()
            ]

    async def merge_issue_metadata(
        self,
        issue_number: int,
        repo: str,
        metadata_update: dict,
    ) -> None:
        """Atomically merge keys into issue_state metadata using server-side JSON merge.

        Unlike upsert_issue_state (which replaces the entire metadata object),
        this merges the provided keys into the existing metadata without a
        read-modify-write cycle, eliminating race conditions.
        """
        import json as json_mod

        async with self._session() as session:
            await session.execute(
                text("""
                    UPDATE issue_state
                    SET metadata = COALESCE(metadata::jsonb, '{}'::jsonb) || :new_metadata::jsonb,
                        updated_at = NOW()
                    WHERE issue_number = :issue_number AND repo = :repo
                """),
                {
                    "issue_number": issue_number,
                    "repo": repo,
                    "new_metadata": json_mod.dumps(metadata_update),
                },
            )
            await session.commit()

    # -------------------------------------------------------------------------
    # Checkpoint operations
    # -------------------------------------------------------------------------

    async def save_checkpoint(self, execution_id: UUID, checkpoint: dict) -> None:
        """Save a checkpoint for an execution."""
        async with self._session() as session:
            await session.execute(
                update(ExecutionModel)
                .where(ExecutionModel.id == execution_id)
                .values(checkpoint=checkpoint)  # SQLAlchemy JSON type handles serialization
            )
            await session.commit()

    async def get_latest_checkpoint(self, issue_id: str) -> dict | None:
        """Get the most recent checkpoint for an issue."""
        async with self._session() as session:
            result = await session.execute(
                select(ExecutionModel.checkpoint)
                .where(ExecutionModel.issue_id == issue_id, ExecutionModel.checkpoint.isnot(None))
                .order_by(ExecutionModel.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_all_checkpoints(self, issue_id: str) -> list[dict]:
        """Get all checkpoints for an issue, newest first."""
        async with self._session() as session:
            result = await session.execute(
                select(
                    ExecutionModel.id,
                    ExecutionModel.checkpoint,
                    ExecutionModel.mode,
                    ExecutionModel.status,
                    ExecutionModel.created_at,
                    ExecutionModel.completed_at,
                )
                .where(ExecutionModel.issue_id == issue_id, ExecutionModel.checkpoint.isnot(None))
                .order_by(ExecutionModel.created_at.desc())
            )
            return [dict(row._mapping) for row in result.all()]

    # -------------------------------------------------------------------------
    # Cron state operations
    # -------------------------------------------------------------------------

    async def get_cron_state(self, key: str) -> dict | None:
        """Get a cron state value."""
        async with self._session() as session:
            result = await session.execute(select(CronStateModel.value).where(CronStateModel.key == key))
            return result.scalar_one_or_none()

    async def set_cron_state(self, key: str, value: dict) -> None:
        """Set a cron state value."""
        async with self._session() as session:
            stmt = pg_insert(CronStateModel).values(
                key=key,
                value=value,  # SQLAlchemy JSON type handles serialization
                updated_at=func.now(),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": value,
                    "updated_at": func.now(),
                },
            )
            await session.execute(stmt)
            await session.commit()

    # -------------------------------------------------------------------------
    # Execution updates for new columns
    # -------------------------------------------------------------------------

    async def set_external_run_id(self, execution_id: UUID, external_run_id: str) -> None:
        """Store the backend-specific run ID (e.g. Oz run ID) for an execution."""
        async with self._session() as session:
            await session.execute(
                update(ExecutionModel).where(ExecutionModel.id == execution_id).values(external_run_id=external_run_id)
            )
            await session.commit()

    # -------------------------------------------------------------------------
    # Pipeline events (audit trail)
    # -------------------------------------------------------------------------

    async def record_pipeline_event(
        self,
        issue_number: int,
        repo: str,
        event_type: str,
        stage: str,
        detail: dict | None = None,
    ) -> None:
        """Append a pipeline event to the audit trail."""
        async with self._session() as session:
            session.add(
                PipelineEventModel(
                    issue_number=issue_number,
                    repo=repo,
                    event_type=event_type,
                    stage=stage,
                    detail=detail,
                )
            )
            await session.commit()

    async def get_pipeline_events(
        self,
        repo: str,
        issue_number: int | None = None,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query pipeline events with optional filters."""
        async with self._session() as session:
            stmt = select(PipelineEventModel).where(PipelineEventModel.repo == repo)
            if issue_number is not None:
                stmt = stmt.where(PipelineEventModel.issue_number == issue_number)
            if event_type is not None:
                stmt = stmt.where(PipelineEventModel.event_type == event_type)
            stmt = stmt.order_by(PipelineEventModel.created_at.desc()).limit(limit).offset(offset)
            result = await session.execute(stmt)
            return [
                {
                    "id": str(m.id),
                    "issue_number": m.issue_number,
                    "repo": m.repo,
                    "event_type": m.event_type,
                    "stage": m.stage,
                    "detail": m.detail,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in result.scalars().all()
            ]

    async def get_pipeline_stats(self, repo: str) -> dict:
        """Get aggregate pipeline statistics for the dashboard."""
        async with self._session() as session:
            # Classifications
            class_stmt = (
                select(IssueStateModel.classification, func.count().label("count"))
                .where(IssueStateModel.repo == repo)
                .group_by(IssueStateModel.classification)
            )
            class_result = await session.execute(class_stmt)
            classifications = {row.classification or "unclassified": row.count for row in class_result.all()}

            # Executions by status (filtered by repo)
            exec_stmt = (
                select(ExecutionModel.status, func.count().label("count"))
                .where(ExecutionModel.repo_url.contains(repo))
                .group_by(ExecutionModel.status)
            )
            exec_result = await session.execute(exec_stmt)
            execution_counts = {row.status: row.count for row in exec_result.all()}

            # Total tracked
            total_result = await session.execute(
                select(func.count()).select_from(IssueStateModel).where(IssueStateModel.repo == repo)
            )

            return {
                "classifications": classifications,
                "execution_counts": execution_counts,
                "total_tracked_issues": total_result.scalar() or 0,
            }

    async def list_all_issue_states(
        self,
        repo: str,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        """List all issue states for the dashboard (paginated)."""
        async with self._session() as session:
            stmt = (
                select(IssueStateModel)
                .where(IssueStateModel.repo == repo)
                .order_by(IssueStateModel.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            return [
                {
                    "issue_number": m.issue_number,
                    "repo": m.repo,
                    "classification": m.classification,
                    "parent_issue": m.parent_issue,
                    "sub_issues": m.sub_issues,
                    "retry_count": m.retry_count,
                    "metadata": m.metadata_,
                    "last_checked_at": m.last_checked_at.isoformat() if m.last_checked_at else None,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "updated_at": m.updated_at.isoformat() if m.updated_at else None,
                }
                for m in result.scalars().all()
            ]

    # -------------------------------------------------------------------------
    # Agent events (execution-level audit trail)
    # -------------------------------------------------------------------------

    async def record_agent_event(
        self,
        execution_id: UUID,
        message_type: str,
        content: str | None = None,
        tool_name: str | None = None,
        tool_id: str | None = None,
    ) -> None:
        """Append an agent chat/tool event to the audit log."""
        async with self._session() as session:
            session.add(
                AgentEventModel(
                    execution_id=execution_id,
                    message_type=message_type,
                    content=content,
                    tool_name=tool_name,
                    tool_id=tool_id,
                )
            )
            await session.commit()

    async def get_agent_events(
        self,
        execution_id: UUID,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        """Get agent events for an execution, oldest first."""
        async with self._session() as session:
            result = await session.execute(
                select(AgentEventModel)
                .where(AgentEventModel.execution_id == execution_id)
                .order_by(AgentEventModel.created_at.asc())
                .limit(limit)
                .offset(offset)
            )
            return [
                {
                    "id": str(m.id),
                    "execution_id": str(m.execution_id),
                    "message_type": m.message_type,
                    "content": m.content,
                    "tool_name": m.tool_name,
                    "tool_id": m.tool_id,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in result.scalars().all()
            ]

    async def list_executions_for_dashboard(
        self,
        issue_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """List executions with all fields for dashboard display."""
        async with self._session() as session:
            result = await session.execute(
                select(ExecutionModel)
                .where(ExecutionModel.issue_id == issue_id)
                .order_by(ExecutionModel.created_at.desc())
                .limit(limit)
            )
            return [
                {
                    "id": str(m.id),
                    "status": m.status,
                    "mode": m.mode,
                    "prompt": m.prompt,
                    "result": m.result,
                    "pr_number": m.pr_number,
                    "branch": m.branch,
                    "external_run_id": m.external_run_id,
                    "session_link": m.session_link,
                    "cost_cents": m.cost_cents,
                    "started_at": m.started_at.isoformat() if m.started_at else None,
                    "completed_at": m.completed_at.isoformat() if m.completed_at else None,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in result.scalars().all()
            ]

    async def list_all_executions_for_dashboard(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List executions across all issues for dashboard display."""
        async with self._session() as session:
            stmt = select(ExecutionModel).order_by(ExecutionModel.created_at.desc())
            if status:
                stmt = stmt.where(ExecutionModel.status == status)
            stmt = stmt.limit(limit).offset(offset)
            result = await session.execute(stmt)
            return [
                {
                    "id": str(m.id),
                    "issue_id": m.issue_id,
                    "status": m.status,
                    "mode": m.mode,
                    "prompt": (m.prompt or "")[:200],
                    "result": (m.result or "")[:200],
                    "pr_number": m.pr_number,
                    "branch": m.branch,
                    "external_run_id": m.external_run_id,
                    "session_link": m.session_link,
                    "cost_cents": m.cost_cents,
                    "started_at": m.started_at.isoformat() if m.started_at else None,
                    "completed_at": m.completed_at.isoformat() if m.completed_at else None,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in result.scalars().all()
            ]

    async def get_execution_counts_by_issue(self) -> dict[str, int]:
        """Return {issue_id: execution_count} for all issues."""
        async with self._session() as session:
            stmt = select(ExecutionModel.issue_id, func.count()).group_by(ExecutionModel.issue_id)
            result = await session.execute(stmt)
            return {row[0]: row[1] for row in result.all()}

    async def set_session_link(self, execution_id: UUID, session_link: str) -> None:
        """Store the Oz session link for an execution."""
        async with self._session() as session:
            await session.execute(
                update(ExecutionModel).where(ExecutionModel.id == execution_id).values(session_link=session_link)
            )
            await session.commit()

    async def set_cost(self, execution_id: UUID, cost_cents: int) -> None:
        """Store the execution cost in cents."""
        async with self._session() as session:
            await session.execute(
                update(ExecutionModel).where(ExecutionModel.id == execution_id).values(cost_cents=cost_cents)
            )
            await session.commit()

    async def get_active_executions_with_external_run_id(self) -> list[tuple[UUID, str]]:
        """Get all pending/running executions that have an external_run_id.

        Used to recover in-flight Oz runs after process restart.
        Returns list of (execution_id, external_run_id) tuples.
        """
        async with self._session() as session:
            result = await session.execute(
                select(ExecutionModel.id, ExecutionModel.external_run_id).where(
                    ExecutionModel.status.in_(["pending", "running"]),
                    ExecutionModel.external_run_id.isnot(None),
                )
            )
            return [(row.id, row.external_run_id) for row in result.all()]

    async def update_execution_result(
        self,
        execution_id: UUID,
        status: ExecutionStatus,
        result: str | None = None,
        pr_number: int | None = None,
        branch: str | None = None,
        checkpoint: dict | None = None,
    ) -> None:
        """Update execution with result details.

        Only overwrites pr_number, branch, and checkpoint when a non-None
        value is provided — avoids clobbering data written by an earlier call.
        """
        values: dict = {
            "status": status.value,
            "result": result,
            "completed_at": func.now(),
        }
        if pr_number is not None:
            values["pr_number"] = pr_number
        if branch is not None:
            values["branch"] = branch
        if checkpoint is not None:
            values["checkpoint"] = checkpoint
        async with self._session() as session:
            await session.execute(update(ExecutionModel).where(ExecutionModel.id == execution_id).values(**values))
            await session.commit()


# Global instance
_database: Database | None = None


def get_database() -> Database:
    """Get the global database instance."""
    global _database
    if _database is None:
        _database = Database()
    return _database
