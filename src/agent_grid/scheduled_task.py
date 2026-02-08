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
    from .config import settings
    from .coordinator.management_loop import ManagementLoop

    is_dry_run = settings.dry_run

    if is_dry_run:
        logger.info("DRY RUN MODE — reads from GitHub, all writes logged only")
        from .dry_run import install_dry_run_wrappers

        install_dry_run_wrappers()

    logger.info("Starting scheduled coordinator cycle...")

    if not is_dry_run:
        from .coordinator.database import get_database

        db = get_database()
        await db.connect()
        logger.info("Database connected")

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
        if not is_dry_run:
            async with pool.acquire() as conn:
                await conn.execute("SELECT pg_advisory_unlock(42)")

            from .fly.machines import get_fly_client

            try:
                fly_client = get_fly_client()
                await fly_client.close()
            except Exception:
                pass

            await db.close()

        from .issue_tracker import get_issue_tracker

        try:
            tracker = get_issue_tracker()
            await tracker.close()
        except Exception:
            pass

        logger.info("Cleanup complete")


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
