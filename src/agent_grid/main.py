"""FastAPI entry point for Agent Grid."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI

from .config import settings
from .coordinator import (
    coordinator_router,
    get_agent_event_logger,
    get_database,
    get_management_loop,
    get_scheduler,
)
from .execution_grid import event_bus
from .issue_tracker import get_issue_tracker, issues_router, webhook_router

# Configure logging for agent event streaming
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Ensure agent event logger is visible
logging.getLogger("agent_grid.agent").setLevel(logging.INFO)


async def _connect_and_start_services(db, logger) -> None:
    """Connect to database, then start services that depend on it."""
    for attempt in range(3):
        try:
            logger.info(f"Connecting to database (attempt {attempt + 1}/3)...")
            await asyncio.wait_for(db.connect(), timeout=30)
            logger.info("Database connected successfully")
            break
        except asyncio.TimeoutError:
            logger.warning(f"Database connection timeout (attempt {attempt + 1})")
        except Exception as e:
            logger.warning(f"Database connection failed (attempt {attempt + 1}): {e}")
        if attempt < 2:
            await asyncio.sleep(5)
    else:
        logger.error("Failed to connect to database after 3 attempts — services not started")
        return

    # Start services only after DB is ready
    scheduler = get_scheduler()
    await scheduler.start()
    logger.info("Scheduler started")

    management_loop = get_management_loop()
    await management_loop.start()
    logger.info("Management loop started")

    agent_logger = get_agent_event_logger()
    await agent_logger.start()

    # Start Oz polling if using Oz backend
    if settings.deployment_mode == "coordinator" and settings.execution_backend == "oz":
        from .execution_grid.oz_grid import get_oz_execution_grid

        oz_grid = get_oz_execution_grid()
        await oz_grid.start_polling()
        logger.info(f"Oz polling started (interval={settings.oz_poll_interval_seconds}s)")

    logger.info("All services started")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    logger = logging.getLogger("agent_grid.startup")
    logger.info("Starting Agent Grid...")

    # Start event bus first (doesn't need DB)
    await event_bus.start()
    logger.info("Event bus started")

    # Connect DB and start services in background so health checks respond immediately
    db = get_database()
    startup_task = asyncio.create_task(_connect_and_start_services(db, logger))

    logger.info("Agent Grid accepting requests (services starting in background)")
    yield

    # Shutdown
    logger.info("Shutting down Agent Grid...")

    # Wait for startup task if still running
    if not startup_task.done():
        startup_task.cancel()
        try:
            await startup_task
        except asyncio.CancelledError:
            pass

    agent_logger = get_agent_event_logger()
    await agent_logger.stop()
    management_loop = get_management_loop()
    await management_loop.stop()
    scheduler = get_scheduler()
    await scheduler.stop()

    # Stop Oz polling and close client if active
    if settings.deployment_mode == "coordinator" and settings.execution_backend == "oz":
        from .execution_grid.oz_grid import get_oz_execution_grid

        oz_grid = get_oz_execution_grid()
        await oz_grid.close()

    await event_bus.stop()

    tracker = get_issue_tracker()
    await tracker.close()

    # Close Fly client if using Fly backend
    if settings.deployment_mode == "coordinator" and settings.execution_backend == "fly":
        from .fly import get_fly_client

        fly_client = get_fly_client()
        await fly_client.close()

    # Close database connection
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
