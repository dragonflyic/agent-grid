"""FastAPI entry point for Agent Grid."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI

from .config import settings
from .coordinator import (
    coordinator_router,
    dashboard_router,
    get_agent_event_logger,
    get_agent_event_persister,
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


async def _setup_claude_credentials() -> None:
    """Write Claude subscription credentials to ~/.claude/.credentials.json."""
    import os

    _logger = logging.getLogger("agent_grid.startup")
    try:
        import boto3

        client = boto3.client("secretsmanager", region_name=settings.aws_region)
        resp = client.get_secret_value(SecretId=settings.claude_credentials_secret)
        creds_dir = os.path.expanduser("~/.claude")
        os.makedirs(creds_dir, exist_ok=True)
        with open(os.path.join(creds_dir, ".credentials.json"), "w") as f:
            f.write(resp["SecretString"])
        _logger.info("Claude subscription credentials loaded from Secrets Manager")
    except Exception as e:
        if settings.anthropic_api_key:
            _logger.info(f"Claude credentials not available ({e}), will use ANTHROPIC_API_KEY")
        else:
            _logger.warning(f"No Claude credentials available: {e}")


async def _connect_and_start_services(db, logger) -> None:
    """Connect to database, then start services that depend on it."""
    # Load Claude subscription credentials from Secrets Manager
    if settings.execution_backend == "claude-code":
        await _setup_claude_credentials()

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

    event_persister = get_agent_event_persister()
    await event_persister.start()

    # Claude Code CLI backend
    if settings.deployment_mode == "coordinator" and settings.execution_backend == "claude-code":
        from .coordinator.claude_code_callbacks import build_claude_code_callbacks
        from .execution_grid.claude_code_grid import get_claude_code_execution_grid
        from .issue_tracker import get_issue_tracker as _get_tracker

        cli_grid = get_claude_code_execution_grid()
        cli_grid.set_callbacks(build_claude_code_callbacks(db, _get_tracker()))
        logger.info("Claude Code CLI execution grid initialized")

    # Start Oz polling if using Oz backend — wire callbacks first
    if settings.deployment_mode == "coordinator" and settings.execution_backend == "oz":
        from .coordinator.oz_callbacks import build_oz_callbacks
        from .execution_grid.oz_grid import get_oz_execution_grid
        from .issue_tracker import get_issue_tracker as _get_tracker

        oz_grid = get_oz_execution_grid()
        oz_grid.set_callbacks(build_oz_callbacks(db, _get_tracker()))
        await oz_grid.start_polling()
        logger.info(f"Oz polling started (interval={settings.oz_poll_interval_seconds}s)")

    app.state.services_ready = True
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
    event_persister = get_agent_event_persister()
    await event_persister.stop()
    management_loop = get_management_loop()
    await management_loop.stop()
    scheduler = get_scheduler()
    await scheduler.stop()

    # Shutdown Claude Code backend
    if settings.deployment_mode == "coordinator" and settings.execution_backend == "claude-code":
        from .execution_grid.claude_code_grid import get_claude_code_execution_grid

        cli_grid = get_claude_code_execution_grid()
        await cli_grid.close()

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
    await db.close()


app = FastAPI(
    title="Agent Grid",
    description="Agent orchestration system for coding agents",
    version="0.1.0",
    lifespan=lifespan,
)

# Register routers
app.include_router(coordinator_router)
app.include_router(dashboard_router)
app.include_router(webhook_router)
app.include_router(issues_router)


_static_dir = Path(__file__).parent / "static"


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "name": "Agent Grid",
        "version": "0.1.0",
        "status": "running",
    }


@app.get("/dashboard")
async def serve_dashboard():
    """Serve the pipeline dashboard UI."""
    from fastapi.responses import FileResponse

    return FileResponse(_static_dir / "dashboard.html")


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
