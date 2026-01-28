"""SQS client for cross-network job communication."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

from ..config import settings

logger = logging.getLogger("agent_grid.sqs")


class JobRequest(BaseModel):
    """Job request message sent to SQS."""

    execution_id: str
    issue_id: str
    repo_url: str
    prompt: str
    created_at: datetime


class JobResult(BaseModel):
    """Job result message sent back via SQS."""

    execution_id: str
    status: str  # "completed" or "failed"
    result: str | None = None
    branch: str | None = None
    completed_at: datetime


class SQSClient:
    """
    SQS client for coordinator-worker communication.

    Provides async-compatible interface for:
    - Publishing job requests (coordinator -> worker)
    - Polling for job requests (worker)
    - Publishing job results (worker -> coordinator)
    - Receiving job results (coordinator)
    """

    def __init__(
        self,
        job_queue_url: str | None = None,
        result_queue_url: str | None = None,
        region: str | None = None,
    ):
        self._job_queue_url = job_queue_url or settings.sqs_job_queue_url
        self._result_queue_url = result_queue_url or settings.sqs_result_queue_url
        self._region = region or settings.aws_region
        self._client: Any = None

    def _get_client(self) -> Any:
        """Get or create the SQS client (lazy initialization)."""
        if self._client is None:
            self._client = boto3.client("sqs", region_name=self._region)
        return self._client

    async def publish_job_request(self, request: JobRequest) -> str:
        """
        Publish a job request to the jobs queue.

        Args:
            request: The job request to publish.

        Returns:
            SQS message ID.
        """
        if not self._job_queue_url:
            raise ValueError("Job queue URL not configured")

        client = self._get_client()

        # Run in thread pool since boto3 is synchronous
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.send_message(
                QueueUrl=self._job_queue_url,
                MessageBody=request.model_dump_json(),
                MessageAttributes={
                    "execution_id": {
                        "DataType": "String",
                        "StringValue": request.execution_id,
                    },
                    "issue_id": {
                        "DataType": "String",
                        "StringValue": request.issue_id,
                    },
                },
            )
        )

        message_id = response["MessageId"]
        logger.info(f"Published job request {request.execution_id} as message {message_id}")
        return message_id

    async def poll_job_requests(
        self,
        max_messages: int = 1,
        wait_time_seconds: int = 20,
    ) -> list[tuple[JobRequest, str]]:
        """
        Poll for job requests from the jobs queue.

        Args:
            max_messages: Maximum messages to receive (1-10).
            wait_time_seconds: Long polling wait time.

        Returns:
            List of (JobRequest, receipt_handle) tuples.
        """
        if not self._job_queue_url:
            raise ValueError("Job queue URL not configured")

        client = self._get_client()
        loop = asyncio.get_event_loop()

        try:
            response = await loop.run_in_executor(
                None,
                lambda: client.receive_message(
                    QueueUrl=self._job_queue_url,
                    MaxNumberOfMessages=max_messages,
                    WaitTimeSeconds=wait_time_seconds,
                    VisibilityTimeout=settings.sqs_visibility_timeout_seconds,
                    MessageAttributeNames=["All"],
                )
            )
        except ClientError as e:
            logger.error(f"Failed to poll job queue: {e}")
            return []

        messages = response.get("Messages", [])
        results = []

        for msg in messages:
            try:
                body = json.loads(msg["Body"])
                request = JobRequest(**body)
                receipt_handle = msg["ReceiptHandle"]
                results.append((request, receipt_handle))
                logger.info(f"Received job request {request.execution_id}")
            except Exception as e:
                logger.error(f"Failed to parse job request: {e}")
                # Delete malformed message to avoid infinite retry
                await self._delete_message(self._job_queue_url, msg["ReceiptHandle"])

        return results

    async def delete_job_request(self, receipt_handle: str) -> None:
        """Delete a job request message after successful processing."""
        if not self._job_queue_url:
            return
        await self._delete_message(self._job_queue_url, receipt_handle)

    async def publish_job_result(self, result: JobResult) -> str:
        """
        Publish a job result to the results queue.

        Args:
            result: The job result to publish.

        Returns:
            SQS message ID.
        """
        if not self._result_queue_url:
            raise ValueError("Result queue URL not configured")

        client = self._get_client()
        loop = asyncio.get_event_loop()

        response = await loop.run_in_executor(
            None,
            lambda: client.send_message(
                QueueUrl=self._result_queue_url,
                MessageBody=result.model_dump_json(),
                MessageAttributes={
                    "execution_id": {
                        "DataType": "String",
                        "StringValue": result.execution_id,
                    },
                    "status": {
                        "DataType": "String",
                        "StringValue": result.status,
                    },
                },
            )
        )

        message_id = response["MessageId"]
        logger.info(f"Published job result {result.execution_id} ({result.status}) as message {message_id}")
        return message_id

    async def poll_job_results(
        self,
        max_messages: int = 10,
        wait_time_seconds: int = 5,
    ) -> list[tuple[JobResult, str]]:
        """
        Poll for job results from the results queue.

        Args:
            max_messages: Maximum messages to receive (1-10).
            wait_time_seconds: Long polling wait time.

        Returns:
            List of (JobResult, receipt_handle) tuples.
        """
        if not self._result_queue_url:
            raise ValueError("Result queue URL not configured")

        client = self._get_client()
        loop = asyncio.get_event_loop()

        try:
            response = await loop.run_in_executor(
                None,
                lambda: client.receive_message(
                    QueueUrl=self._result_queue_url,
                    MaxNumberOfMessages=max_messages,
                    WaitTimeSeconds=wait_time_seconds,
                    MessageAttributeNames=["All"],
                )
            )
        except ClientError as e:
            logger.error(f"Failed to poll result queue: {e}")
            return []

        messages = response.get("Messages", [])
        results = []

        for msg in messages:
            try:
                body = json.loads(msg["Body"])
                result = JobResult(**body)
                receipt_handle = msg["ReceiptHandle"]
                results.append((result, receipt_handle))
                logger.info(f"Received job result {result.execution_id} ({result.status})")
            except Exception as e:
                logger.error(f"Failed to parse job result: {e}")
                await self._delete_message(self._result_queue_url, msg["ReceiptHandle"])

        return results

    async def delete_job_result(self, receipt_handle: str) -> None:
        """Delete a job result message after processing."""
        if not self._result_queue_url:
            return
        await self._delete_message(self._result_queue_url, receipt_handle)

    async def _delete_message(self, queue_url: str, receipt_handle: str) -> None:
        """Delete a message from a queue."""
        client = self._get_client()
        loop = asyncio.get_event_loop()

        try:
            await loop.run_in_executor(
                None,
                lambda: client.delete_message(
                    QueueUrl=queue_url,
                    ReceiptHandle=receipt_handle,
                )
            )
        except ClientError as e:
            logger.error(f"Failed to delete message: {e}")


# Global instance
_sqs_client: SQSClient | None = None


def get_sqs_client() -> SQSClient:
    """Get the global SQS client instance."""
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = SQSClient()
    return _sqs_client
