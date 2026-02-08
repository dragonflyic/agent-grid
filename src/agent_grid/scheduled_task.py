"""Entrypoint for scheduled ECS task execution.

Runs a single management loop cycle and exits.
Used by EventBridge → ECS Fargate scheduled task.
"""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent_grid.scheduled_task")


async def main() -> int:
    from .coordinator.database import get_database
    from .coordinator.management_loop import ManagementLoop

    logger.info("Starting scheduled coordinator cycle...")

    db = get_database()
    await db.connect()
    logger.info("Database connected")

    # Advisory lock prevents overlapping runs if previous cycle is still going
    pool = db._pool
    async with pool.acquire() as conn:
        acquired = await conn.fetchval("SELECT pg_try_advisory_lock(42)")
        if not acquired:
            logger.info("Another cycle is already running, exiting")
            return 0

    try:
        loop = ManagementLoop()
        await loop.run_once()
        logger.info("Scheduled cycle completed successfully")
        return 0
    except Exception:
        logger.exception("Scheduled cycle failed")
        return 1
    finally:
        # Release advisory lock
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_unlock(42)")

        # Cleanup
        from .fly.machines import get_fly_client

        try:
            fly_client = get_fly_client()
            await fly_client.close()
        except Exception:
            pass

        from .issue_tracker import get_issue_tracker

        try:
            tracker = get_issue_tracker()
            await tracker.close()
        except Exception:
            pass

        await db.close()
        logger.info("Cleanup complete")


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
