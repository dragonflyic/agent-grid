"""
Local worker process for running agents.

This is designed to run on your desktop, polling SQS for jobs
and executing agents locally where Claude Code runs natively.

Usage:
    poetry run python -m agent_grid.worker

Environment variables:
    AGENT_GRID_SQS_JOB_QUEUE_URL: URL of the jobs queue
    AGENT_GRID_SQS_RESULT_QUEUE_URL: URL of the results queue
    AGENT_GRID_AWS_REGION: AWS region (default: us-west-2)
    AWS_PROFILE: AWS credentials profile (recommended for local use)
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .config import settings
from .execution_grid.sqs_client import (
    get_sqs_client,
    JobRequest,
    JobResult,
)
from .execution_grid.public_api import AgentExecution, ExecutionStatus, utc_now
from .execution_grid.agent_runner import AgentRunner
from .execution_grid.repo_manager import get_repo_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agent_grid.worker")


class Worker:
    """
    Local worker that polls SQS for jobs and runs agents.

    Designed to run indefinitely on your desktop.
    """

    def __init__(self):
        self._sqs = get_sqs_client()
        self._agent_runner = AgentRunner()
        self._repo_manager = get_repo_manager()
        self._running = False
        self._current_job: JobRequest | None = None
        self._current_receipt: str | None = None

    async def start(self) -> None:
        """Start the worker loop."""
        logger.info("=" * 60)
        logger.info("Agent Grid Worker Starting")
        logger.info("=" * 60)
        logger.info(f"Job Queue: {settings.sqs_job_queue_url}")
        logger.info(f"Result Queue: {settings.sqs_result_queue_url}")
        logger.info(f"Region: {settings.aws_region}")
        logger.info(f"Poll Interval: {settings.sqs_poll_interval_seconds}s")
        logger.info(f"Repo Base Path: {settings.repo_base_path}")
        logger.info("=" * 60)

        if not settings.sqs_job_queue_url or not settings.sqs_result_queue_url:
            logger.error("SQS queue URLs not configured!")
            logger.error("Set AGENT_GRID_SQS_JOB_QUEUE_URL and AGENT_GRID_SQS_RESULT_QUEUE_URL")
            return

        self._running = True

        while self._running:
            try:
                await self._poll_and_execute()
            except Exception as e:
                logger.exception(f"Error in worker loop: {e}")
                # Don't spam on errors, wait before retrying
                await asyncio.sleep(5)

        logger.info("Worker stopped")

    async def stop(self) -> None:
        """Stop the worker gracefully."""
        logger.info("Stopping worker...")
        self._running = False

        # If we have a job in progress, let it finish
        if self._current_job:
            logger.info(f"Waiting for current job {self._current_job.execution_id} to complete...")

    async def _poll_and_execute(self) -> None:
        """Poll for a job and execute it."""
        logger.debug("Polling for jobs...")

        jobs = await self._sqs.poll_job_requests(
            max_messages=1,
            wait_time_seconds=20,  # Long polling
        )

        if not jobs:
            return

        job_request, receipt_handle = jobs[0]
        self._current_job = job_request
        self._current_receipt = receipt_handle

        logger.info(f"Processing job: {job_request.execution_id}")
        logger.info(f"  Issue: {job_request.issue_id}")
        logger.info(f"  Repo: {job_request.repo_url}")

        try:
            result = await self._execute_job(job_request)

            # Publish result
            await self._sqs.publish_job_result(result)

            # Delete job from queue (acknowledge successful processing)
            await self._sqs.delete_job_request(receipt_handle)

            logger.info(f"Job {job_request.execution_id} completed with status: {result.status}")

        except Exception as e:
            logger.exception(f"Job {job_request.execution_id} failed: {e}")

            # Publish failure result
            failure_result = JobResult(
                execution_id=job_request.execution_id,
                status="failed",
                result=str(e),
                completed_at=utc_now(),
            )
            await self._sqs.publish_job_result(failure_result)

            # Delete job from queue (we've reported the failure)
            await self._sqs.delete_job_request(receipt_handle)

        finally:
            self._current_job = None
            self._current_receipt = None

    async def _execute_job(self, job_request: JobRequest) -> JobResult:
        """
        Execute a job request using the agent runner.

        Args:
            job_request: The job to execute.

        Returns:
            JobResult with execution outcome.
        """
        execution_id = UUID(job_request.execution_id)

        # Create execution record
        execution = AgentExecution(
            id=execution_id,
            issue_id=job_request.issue_id,
            repo_url=job_request.repo_url,
            prompt=job_request.prompt,
        )

        # Run the agent (this does the full clone -> execute -> push cycle)
        result_execution = await self._agent_runner.run(execution, job_request.prompt)

        # Extract branch name
        branch_name = f"agent/{job_request.issue_id}"

        return JobResult(
            execution_id=job_request.execution_id,
            status="completed" if result_execution.status == ExecutionStatus.COMPLETED else "failed",
            result=result_execution.result,
            branch=branch_name if result_execution.status == ExecutionStatus.COMPLETED else None,
            completed_at=utc_now(),
        )


async def async_main() -> None:
    """Async entry point."""
    worker = Worker()

    # Set up signal handlers for graceful shutdown
    loop = asyncio.get_event_loop()

    def signal_handler() -> None:
        logger.info("Received shutdown signal")
        asyncio.create_task(worker.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    await worker.start()


def main() -> None:
    """Entry point for the worker."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Worker interrupted")
        sys.exit(0)


if __name__ == "__main__":
    main()
