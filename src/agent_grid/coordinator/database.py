"""PostgreSQL database access for coordinator."""

from datetime import datetime
from uuid import UUID

import asyncpg

from ..common.models import AgentExecution, ExecutionStatus, NudgeRequest, utc_now
from ..config import settings


class Database:
    """PostgreSQL database interface for coordinator operations."""

    def __init__(self, database_url: str | None = None):
        self._database_url = database_url or settings.database_url
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Establish database connection pool."""
        self._pool = await asyncpg.create_pool(
            self._database_url,
            min_size=2,
            max_size=10,
        )

    async def close(self) -> None:
        """Close database connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def _get_pool(self) -> asyncpg.Pool:
        """Get the connection pool, connecting if needed."""
        if self._pool is None:
            await self.connect()
        return self._pool  # type: ignore

    # Execution operations

    async def create_execution(self, execution: AgentExecution) -> None:
        """Insert a new execution record."""
        pool = await self._get_pool()
        await pool.execute(
            """
            INSERT INTO executions (id, issue_id, repo_url, status, prompt, result, started_at, completed_at, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            execution.id,
            execution.issue_id,
            execution.repo_url,
            execution.status.value,
            execution.prompt,
            execution.result,
            execution.started_at,
            execution.completed_at,
            execution.created_at,
        )

    async def update_execution(self, execution: AgentExecution) -> None:
        """Update an existing execution record."""
        pool = await self._get_pool()
        await pool.execute(
            """
            UPDATE executions
            SET status = $2, prompt = $3, result = $4, started_at = $5, completed_at = $6
            WHERE id = $1
            """,
            execution.id,
            execution.status.value,
            execution.prompt,
            execution.result,
            execution.started_at,
            execution.completed_at,
        )

    async def get_execution(self, execution_id: UUID) -> AgentExecution | None:
        """Get an execution by ID."""
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM executions WHERE id = $1",
            execution_id,
        )
        if row:
            return self._row_to_execution(row)
        return None

    async def list_executions(
        self,
        status: ExecutionStatus | None = None,
        issue_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentExecution]:
        """List executions with optional filters."""
        pool = await self._get_pool()

        query = "SELECT * FROM executions WHERE 1=1"
        params: list = []
        param_num = 1

        if status:
            query += f" AND status = ${param_num}"
            params.append(status.value)
            param_num += 1

        if issue_id:
            query += f" AND issue_id = ${param_num}"
            params.append(issue_id)
            param_num += 1

        query += f" ORDER BY created_at DESC LIMIT ${param_num} OFFSET ${param_num + 1}"
        params.extend([limit, offset])

        rows = await pool.fetch(query, *params)
        return [self._row_to_execution(row) for row in rows]

    async def get_running_executions(self) -> list[AgentExecution]:
        """Get all currently running executions."""
        return await self.list_executions(status=ExecutionStatus.RUNNING)

    async def get_execution_for_issue(self, issue_id: str) -> AgentExecution | None:
        """Get the most recent execution for an issue."""
        pool = await self._get_pool()
        row = await pool.fetchrow(
            """
            SELECT * FROM executions
            WHERE issue_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            issue_id,
        )
        if row:
            return self._row_to_execution(row)
        return None

    def _row_to_execution(self, row: asyncpg.Record) -> AgentExecution:
        """Convert a database row to an AgentExecution."""
        return AgentExecution(
            id=row["id"],
            issue_id=row["issue_id"],
            repo_url=row["repo_url"],
            status=ExecutionStatus(row["status"]),
            prompt=row["prompt"],
            result=row["result"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
        )

    # Nudge queue operations

    async def create_nudge(self, nudge: NudgeRequest) -> None:
        """Insert a new nudge request."""
        pool = await self._get_pool()
        await pool.execute(
            """
            INSERT INTO nudge_queue (id, issue_id, source_execution_id, priority, created_at, processed_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            nudge.id,
            nudge.issue_id,
            nudge.source_execution_id,
            nudge.priority,
            nudge.created_at,
            nudge.processed_at,
        )

    async def get_pending_nudges(self, limit: int = 10) -> list[NudgeRequest]:
        """Get pending nudge requests ordered by priority."""
        pool = await self._get_pool()
        rows = await pool.fetch(
            """
            SELECT * FROM nudge_queue
            WHERE processed_at IS NULL
            ORDER BY priority DESC, created_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [self._row_to_nudge(row) for row in rows]

    async def mark_nudge_processed(self, nudge_id: UUID) -> None:
        """Mark a nudge request as processed."""
        pool = await self._get_pool()
        await pool.execute(
            """
            UPDATE nudge_queue
            SET processed_at = $2
            WHERE id = $1
            """,
            nudge_id,
            utc_now(),
        )

    def _row_to_nudge(self, row: asyncpg.Record) -> NudgeRequest:
        """Convert a database row to a NudgeRequest."""
        return NudgeRequest(
            id=row["id"],
            issue_id=row["issue_id"],
            source_execution_id=row["source_execution_id"],
            priority=row["priority"],
            created_at=row["created_at"],
            processed_at=row["processed_at"],
        )

    # Budget tracking operations

    async def record_budget_usage(
        self,
        execution_id: UUID,
        tokens_used: int,
        duration_seconds: int,
    ) -> None:
        """Record budget usage for an execution."""
        pool = await self._get_pool()
        await pool.execute(
            """
            INSERT INTO budget_usage (id, execution_id, tokens_used, duration_seconds, recorded_at)
            VALUES (gen_random_uuid(), $1, $2, $3, NOW())
            """,
            execution_id,
            tokens_used,
            duration_seconds,
        )

    async def get_total_budget_usage(
        self,
        since: datetime | None = None,
    ) -> dict[str, int]:
        """Get total budget usage."""
        pool = await self._get_pool()

        query = "SELECT COALESCE(SUM(tokens_used), 0) as tokens, COALESCE(SUM(duration_seconds), 0) as duration FROM budget_usage"
        params: list = []

        if since:
            query += " WHERE recorded_at >= $1"
            params.append(since)

        row = await pool.fetchrow(query, *params)
        return {
            "tokens_used": row["tokens"] if row else 0,
            "duration_seconds": row["duration"] if row else 0,
        }


# Global instance
_database: Database | None = None


def get_database() -> Database:
    """Get the global database instance."""
    global _database
    if _database is None:
        _database = Database()
    return _database
