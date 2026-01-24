"""Configuration management for Agent Grid."""

from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "postgresql://postgres:dev@localhost:5432/agent_grid"

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

    model_config = {"env_prefix": "AGENT_GRID_"}


settings = Settings()
