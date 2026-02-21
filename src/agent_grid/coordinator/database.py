"""PostgreSQL database access for coordinator."""

import json
from datetime import datetime
from uuid import UUID

import asyncpg

from ..config import settings
from ..execution_grid import AgentExecution, ExecutionStatus
from .public_api import NudgeRequest, utc_now


class Database:
    """PostgreSQL database interface for coordinator operations."""

    def __init__(self, database_url: str | None = None):
        self._database_url = database_url or settings.database_url
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Establish database connection pool.

        Note: Schema is managed by Alembic migrations. Run migrations before
        starting the application:
            alembic upgrade head
        """
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

    # -------------------------------------------------------------------------
    # Query helpers — build SQL from dicts so column↔value mapping is obvious
    # -------------------------------------------------------------------------

    @staticmethod
    def _build_insert(table: str, data: dict) -> tuple[str, list]:
        """Build an INSERT query from a dict of {column: value}.

        Returns (sql, params) ready for pool.execute(sql, *params).
        """
        cols = ", ".join(data.keys())
        placeholders = ", ".join(f"${i}" for i in range(1, len(data) + 1))
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        return sql, list(data.values())

    @staticmethod
    def _build_update(table: str, data: dict, where: dict) -> tuple[str, list]:
        """Build an UPDATE query from dicts.

        data:  {column: value} for SET clause
        where: {column: value} for WHERE clause

        Returns (sql, params) ready for pool.execute(sql, *params).
        """
        params: list = []
        idx = 1

        set_parts = []
        for col, val in data.items():
            set_parts.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1

        where_parts = []
        for col, val in where.items():
            where_parts.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1

        sql = f"UPDATE {table} SET {', '.join(set_parts)} WHERE {' AND '.join(where_parts)}"
        return sql, params

    # -------------------------------------------------------------------------
    # Execution operations
    # -------------------------------------------------------------------------

    def _execution_row(self, execution: AgentExecution, issue_id: str) -> dict:
        """Build column→value dict for an execution INSERT."""
        return {
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
        }

    async def create_execution(self, execution: AgentExecution, issue_id: str) -> None:
        """Insert a new execution record."""
        pool = await self._get_pool()
        data = self._execution_row(execution, issue_id)
        sql, params = self._build_insert("executions", data)
        await pool.execute(sql, *params)

    async def try_claim_issue(self, execution: AgentExecution, issue_id: str) -> bool:
        """Atomically claim an issue for execution, preventing duplicates.

        Returns True if the claim succeeded (no other active execution exists).
        Returns False if another pending/running execution already exists.

        Uses both WHERE NOT EXISTS check and the partial unique index
        (idx_executions_active_issue) as a safety net for concurrent claims.
        """
        pool = await self._get_pool()
        data = self._execution_row(execution, issue_id)
        cols = ", ".join(data.keys())
        placeholders = ", ".join(f"${i}" for i in range(1, len(data) + 1))
        # $2 is issue_id (second key in _execution_row)
        issue_id_param = "$2"

        sql = f"""
            INSERT INTO executions ({cols})
            SELECT {placeholders}
            WHERE NOT EXISTS (
                SELECT 1 FROM executions
                WHERE issue_id = {issue_id_param} AND status IN ('pending', 'running')
            )
            RETURNING id
        """
        try:
            result = await pool.fetchval(sql, *data.values())
            return result is not None
        except asyncpg.UniqueViolationError:
            return False

    async def update_execution(self, execution: AgentExecution) -> None:
        """Update an existing execution record."""
        pool = await self._get_pool()
        sql, params = self._build_update(
            "executions",
            data={
                "status": execution.status.value,
                "prompt": execution.prompt,
                "result": execution.result,
                "started_at": execution.started_at,
                "completed_at": execution.completed_at,
            },
            where={"id": execution.id},
        )
        await pool.execute(sql, *params)

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

    async def get_issue_id_for_execution(self, execution_id: UUID) -> str | None:
        """Get the issue_id associated with an execution."""
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT issue_id FROM executions WHERE id = $1",
            execution_id,
        )
        return row["issue_id"] if row else None

    def _row_to_execution(self, row: asyncpg.Record) -> AgentExecution:
        """Convert a database row to an AgentExecution."""
        return AgentExecution(
            id=row["id"],
            repo_url=row["repo_url"],
            status=ExecutionStatus(row["status"]),
            prompt=row["prompt"],
            result=row["result"],
            mode=row.get("mode"),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
        )

    # -------------------------------------------------------------------------
    # Nudge queue operations
    # -------------------------------------------------------------------------

    async def create_nudge(self, nudge: NudgeRequest) -> None:
        """Insert a new nudge request."""
        pool = await self._get_pool()
        data = {
            "id": nudge.id,
            "issue_id": nudge.issue_id,
            "source_execution_id": nudge.source_execution_id,
            "priority": nudge.priority,
            "created_at": nudge.created_at,
            "processed_at": nudge.processed_at,
        }
        sql, params = self._build_insert("nudge_queue", data)
        await pool.execute(sql, *params)

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
        sql, params = self._build_update(
            "nudge_queue",
            data={"processed_at": utc_now()},
            where={"id": nudge_id},
        )
        await pool.execute(sql, *params)

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
        pool = await self._get_pool()
        # id uses gen_random_uuid() and recorded_at uses NOW() — both SQL functions,
        # so this can't use _build_insert
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

        query = (
            "SELECT COALESCE(SUM(tokens_used), 0) as tokens,"
            " COALESCE(SUM(duration_seconds), 0) as duration FROM budget_usage"
        )
        params: list = []

        if since:
            query += " WHERE recorded_at >= $1"
            params.append(since)

        row = await pool.fetchrow(query, *params)
        return {
            "tokens_used": row["tokens"] if row else 0,
            "duration_seconds": row["duration"] if row else 0,
        }

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
        pool = await self._get_pool()
        data = {
            "issue_number": issue_number,
            "repo": repo,
            "classification": classification,
            "parent_issue": parent_issue,
            "sub_issues": sub_issues,
            "retry_count": retry_count,
            "metadata": json.dumps(metadata) if metadata is not None else None,
        }
        cols = ", ".join(data.keys())
        placeholders = ", ".join(f"${i}" for i in range(1, len(data) + 1))

        # Positional params follow dict key order:
        # $1=issue_number, $2=repo, $3=classification, $4=parent_issue,
        # $5=sub_issues, $6=retry_count, $7=metadata
        await pool.execute(
            f"""
            INSERT INTO issue_state
            ({cols}, last_checked_at, updated_at)
            VALUES ({placeholders}, NOW(), NOW())
            ON CONFLICT (issue_number, repo) DO UPDATE SET
                classification = COALESCE($3, issue_state.classification),
                parent_issue = COALESCE($4, issue_state.parent_issue),
                sub_issues = COALESCE($5, issue_state.sub_issues),
                retry_count = $6,
                metadata = COALESCE($7, issue_state.metadata),
                last_checked_at = NOW(),
                updated_at = NOW()
            """,
            *data.values(),
        )

    async def get_issue_state(self, issue_number: int, repo: str) -> dict | None:
        """Get issue state by number and repo."""
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT * FROM issue_state WHERE issue_number = $1 AND repo = $2",
            issue_number,
            repo,
        )
        return dict(row) if row else None

    async def list_issue_states(self, repo: str, classification: str | None = None) -> list[dict]:
        """List issue states with optional classification filter."""
        pool = await self._get_pool()
        if classification:
            rows = await pool.fetch(
                "SELECT * FROM issue_state WHERE repo = $1 AND classification = $2",
                repo,
                classification,
            )
        else:
            rows = await pool.fetch(
                "SELECT * FROM issue_state WHERE repo = $1",
                repo,
            )
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # Checkpoint operations
    # -------------------------------------------------------------------------

    async def save_checkpoint(self, execution_id: UUID, checkpoint: dict) -> None:
        """Save a checkpoint for an execution."""
        pool = await self._get_pool()
        sql, params = self._build_update(
            "executions",
            data={"checkpoint": json.dumps(checkpoint)},
            where={"id": execution_id},
        )
        await pool.execute(sql, *params)

    async def get_latest_checkpoint(self, issue_id: str) -> dict | None:
        """Get the most recent checkpoint for an issue."""
        pool = await self._get_pool()
        row = await pool.fetchrow(
            """
            SELECT checkpoint FROM executions
            WHERE issue_id = $1 AND checkpoint IS NOT NULL
            ORDER BY created_at DESC LIMIT 1
            """,
            issue_id,
        )
        if row and row["checkpoint"]:
            return json.loads(row["checkpoint"]) if isinstance(row["checkpoint"], str) else row["checkpoint"]
        return None

    async def get_all_checkpoints(self, issue_id: str) -> list[dict]:
        """Get all checkpoints for an issue, newest first."""
        pool = await self._get_pool()
        rows = await pool.fetch(
            """
            SELECT id, checkpoint, mode, status, created_at, completed_at
            FROM executions
            WHERE issue_id = $1 AND checkpoint IS NOT NULL
            ORDER BY created_at DESC
            """,
            issue_id,
        )
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # Cron state operations
    # -------------------------------------------------------------------------

    async def get_cron_state(self, key: str) -> dict | None:
        """Get a cron state value."""
        pool = await self._get_pool()
        row = await pool.fetchrow("SELECT value FROM cron_state WHERE key = $1", key)
        if row and row["value"]:
            return json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        return None

    async def set_cron_state(self, key: str, value: dict) -> None:
        """Set a cron state value."""
        pool = await self._get_pool()
        await pool.execute(
            """
            INSERT INTO cron_state (key, value, updated_at) VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
            """,
            key,
            json.dumps(value),
        )

    # -------------------------------------------------------------------------
    # Execution updates for new columns
    # -------------------------------------------------------------------------

    async def set_external_run_id(self, execution_id: UUID, external_run_id: str) -> None:
        """Store the backend-specific run ID (e.g. Oz run ID) for an execution."""
        pool = await self._get_pool()
        sql, params = self._build_update(
            "executions",
            data={"external_run_id": external_run_id},
            where={"id": execution_id},
        )
        await pool.execute(sql, *params)

    async def get_active_executions_with_external_run_id(self) -> list[tuple[UUID, str]]:
        """Get all pending/running executions that have an external_run_id.

        Used to recover in-flight Oz runs after process restart.
        Returns list of (execution_id, external_run_id) tuples.
        """
        pool = await self._get_pool()
        rows = await pool.fetch(
            """
            SELECT id, external_run_id FROM executions
            WHERE status IN ('pending', 'running')
            AND external_run_id IS NOT NULL
            """,
        )
        return [(row["id"], row["external_run_id"]) for row in rows]

    async def update_execution_result(
        self,
        execution_id: UUID,
        status: ExecutionStatus,
        result: str | None = None,
        pr_number: int | None = None,
        branch: str | None = None,
        checkpoint: dict | None = None,
    ) -> None:
        """Update execution with result details."""
        pool = await self._get_pool()
        data = {
            "status": status.value,
            "result": result,
            "pr_number": pr_number,
            "branch": branch,
            "checkpoint": json.dumps(checkpoint) if checkpoint else None,
        }
        # completed_at uses NOW() so we handle it in raw SQL
        set_parts = []
        params: list = []
        idx = 1
        for col, val in data.items():
            set_parts.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1
        set_parts.append("completed_at = NOW()")
        params.append(execution_id)

        sql = f"UPDATE executions SET {', '.join(set_parts)} WHERE id = ${idx}"
        await pool.execute(sql, *params)


# Global instance
_database: Database | None = None


def get_database() -> Database:
    """Get the global database instance."""
    global _database
    if _database is None:
        _database = Database()
    return _database
