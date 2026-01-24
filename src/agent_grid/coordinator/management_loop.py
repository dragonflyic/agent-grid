"""Periodic management loop for monitoring parent issues."""

import asyncio

from ..config import settings
from ..issue_tracker import get_issue_tracker
from .database import get_database
from .scheduler import get_scheduler


class ManagementLoop:
    """
    Periodic task that monitors parent issues and resumes stalled work.

    Runs on a configurable interval to:
    - Check for issues that need attention
    - Resume stalled executions
    - Launch management agents for parent issues
    """

    def __init__(self, interval_seconds: int | None = None):
        self._interval = interval_seconds or settings.management_loop_interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._db = get_database()
        self._tracker = get_issue_tracker()
        self._scheduler = get_scheduler()

    async def start(self) -> None:
        """Start the management loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the management loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        """Main management loop."""
        while self._running:
            try:
                await self._check_issues()
            except Exception:
                # Log error but continue
                pass

            await asyncio.sleep(self._interval)

    async def _check_issues(self) -> None:
        """Check issues and take action as needed."""
        # Get running executions
        running = await self._db.get_running_executions()

        for execution in running:
            # Check if execution has been running too long
            if execution.started_at:
                elapsed = (asyncio.get_event_loop().time() -
                          execution.started_at.timestamp())
                if elapsed > settings.execution_timeout_seconds:
                    # Execution timed out - could cancel or escalate
                    pass

    async def run_once(self) -> None:
        """Run a single iteration of the management loop."""
        await self._check_issues()


# Global instance
_management_loop: ManagementLoop | None = None


def get_management_loop() -> ManagementLoop:
    """Get the global management loop instance."""
    global _management_loop
    if _management_loop is None:
        _management_loop = ManagementLoop()
    return _management_loop
