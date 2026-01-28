"""Configuration management for Agent Grid."""

from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "postgresql://postgres:dev@localhost:5433/agent_grid"

    # Issue tracker
    issue_tracker_type: Literal["github", "filesystem"] = "filesystem"
    issues_directory: str = "./issues"

    # GitHub (only used when issue_tracker_type is "github")
    github_token: str = ""
    github_webhook_secret: str = ""

    # Execution limits
    max_concurrent_executions: int = 5
    execution_timeout_seconds: int = 3600  # 1 hour

    # Repo management
    repo_base_path: str = "/tmp/agent-grid"
    cleanup_on_success: bool = True
    cleanup_on_failure: bool = False

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Event bus
    event_bus_max_size: int = 1000

    # Management loop
    management_loop_interval_seconds: int = 300  # 5 minutes

    # Agent execution
    agent_bypass_permissions: bool = True  # Use bypassPermissions mode for autonomous agents (required for non-interactive execution)

    # Testing overrides (for development/testing only)
    test_force_planning_only: bool = False  # Force agents to only create subissues, not write code

    # AWS/SQS Configuration (for hybrid deployment)
    aws_region: str = "us-west-2"
    sqs_job_queue_url: str = ""  # URL for job-requests queue
    sqs_result_queue_url: str = ""  # URL for job-results queue
    sqs_poll_interval_seconds: int = 5  # How often worker polls for jobs
    sqs_visibility_timeout_seconds: int = 3600  # Match execution timeout

    # Deployment mode
    deployment_mode: Literal["local", "coordinator", "worker"] = "local"
    # local: In-memory event bus, everything runs in same process (development)
    # coordinator: Publishes jobs to SQS, listens for results (cloud)
    # worker: Polls SQS for jobs, runs agents locally (desktop)

    model_config = {"env_prefix": "AGENT_GRID_"}


settings = Settings()
