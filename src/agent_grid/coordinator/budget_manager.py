"""Budget and safety controls for agent executions."""

from ..config import settings
from ..execution_grid import ExecutionStatus
from .database import get_database


class BudgetManager:
    """
    Manages safety and budget controls for agent executions.

    Enforces limits on concurrent executions and resource usage.
    """

    def __init__(self, max_concurrent: int | None = None):
        self._max_concurrent = max_concurrent or settings.max_concurrent_executions
        self._db = get_database()

    async def can_launch_agent(self) -> tuple[bool, str | None]:
        """
        Check if a new agent can be launched.

        Returns:
            Tuple of (allowed, reason_if_not_allowed).
        """
        # Check concurrent execution limit
        running = await self._db.list_executions(status=ExecutionStatus.RUNNING)
        if len(running) >= self._max_concurrent:
            return False, f"Max concurrent executions ({self._max_concurrent}) reached"

        return True, None

    async def get_concurrent_count(self) -> int:
        """Get the number of currently running executions."""
        running = await self._db.list_executions(status=ExecutionStatus.RUNNING)
        return len(running)

    async def get_budget_status(self) -> dict:
        """Get current budget status and limits."""
        concurrent = await self.get_concurrent_count()
        usage = await self._db.get_total_budget_usage()

        return {
            "concurrent_executions": concurrent,
            "max_concurrent": self._max_concurrent,
            "tokens_used": usage["tokens_used"],
            "duration_seconds": usage["duration_seconds"],
        }


# Global instance
_budget_manager: BudgetManager | None = None


def get_budget_manager() -> BudgetManager:
    """Get the global budget manager instance."""
    global _budget_manager
    if _budget_manager is None:
        _budget_manager = BudgetManager()
    return _budget_manager
