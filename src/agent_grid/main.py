"""FastAPI entry point for Agent Grid."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI

# Configure logging for agent event streaming
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Ensure agent event logger is visible
logging.getLogger("agent_grid.agent").setLevel(logging.INFO)

from .execution_grid import event_bus, get_execution_grid
from .config import settings
from .coordinator import (
    coordinator_router,
    get_database,
    get_management_loop,
    get_scheduler,
    get_agent_event_logger,
    get_webhook_processor,
)
from .issue_tracker import webhook_router, issues_router, get_issue_tracker


async def _connect_database_background(db, logger) -> None:
    """Connect to database in background with retries."""
    for attempt in range(3):
        try:
            logger.info(f"Connecting to database (attempt {attempt + 1}/3)...")
            await asyncio.wait_for(db.connect(), timeout=30)
            logger.info("Database connected successfully")
            return
        except asyncio.TimeoutError:
            logger.warning(f"Database connection timeout (attempt {attempt + 1})")
        except Exception as e:
            logger.warning(f"Database connection failed (attempt {attempt + 1}): {e}")
        if attempt < 2:
            await asyncio.sleep(5)
    logger.error("Failed to connect to database after 3 attempts")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    logger = logging.getLogger("agent_grid.startup")
    logger.info("Starting Agent Grid...")

    # Start event bus first (doesn't need DB)
    await event_bus.start()
    logger.info("Event bus started")

    # Start database connection in background (don't block health checks)
    db = get_database()
    db_task = asyncio.create_task(_connect_database_background(db, logger))

    # Start SQS grid result listener if in coordinator mode
    if settings.deployment_mode == "coordinator":
        grid = get_execution_grid()
        if hasattr(grid, "start"):
            await grid.start()
            logger.info("SQS grid started")

    # Start scheduler
    scheduler = get_scheduler()
    await scheduler.start()
    logger.info("Scheduler started")

    # Start management loop
    management_loop = get_management_loop()
    await management_loop.start()

    # Start agent event logger for real-time streaming
    agent_logger = get_agent_event_logger()
    await agent_logger.start()

    # Start webhook processor for deduplication
    webhook_processor = get_webhook_processor()
    await webhook_processor.start()
    logger.info("Webhook processor started")

    logger.info("Agent Grid startup complete")
    yield

    # Shutdown
    logger.info("Shutting down Agent Grid...")
    await webhook_processor.stop()
    await agent_logger.stop()
    await management_loop.stop()
    await scheduler.stop()

    # Stop SQS grid if in coordinator mode
    if settings.deployment_mode == "coordinator":
        grid = get_execution_grid()
        if hasattr(grid, "stop"):
            await grid.stop()

    await event_bus.stop()

    tracker = get_issue_tracker()
    await tracker.close()

    # Wait for db connection task and close if connected
    if not db_task.done():
        db_task.cancel()
        try:
            await db_task
        except asyncio.CancelledError:
            pass
    if db._pool is not None:
        await db.close()


app = FastAPI(
    title="Agent Grid",
    description="Agent orchestration system for coding agents",
    version="0.1.0",
    lifespan=lifespan,
)

# Register routers
app.include_router(coordinator_router)
app.include_router(webhook_router)
app.include_router(issues_router)


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "name": "Agent Grid",
        "version": "0.1.0",
        "status": "running",
    }


def run() -> None:
    """Run the application."""
    uvicorn.run(
        "agent_grid.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
