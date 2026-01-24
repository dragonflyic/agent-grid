"""FastAPI entry point for Agent Grid."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI

from .common import event_bus
from .config import settings
from .coordinator import (
    coordinator_router,
    get_database,
    get_management_loop,
    get_scheduler,
)
from .issue_tracker import webhook_router, get_issue_tracker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    # Startup
    db = get_database()
    await db.connect()

    # Start event bus
    await event_bus.start()

    # Start scheduler
    scheduler = get_scheduler()
    await scheduler.start()

    # Start management loop
    management_loop = get_management_loop()
    await management_loop.start()

    yield

    # Shutdown
    await management_loop.stop()
    await scheduler.stop()
    await event_bus.stop()

    tracker = get_issue_tracker()
    await tracker.close()

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
