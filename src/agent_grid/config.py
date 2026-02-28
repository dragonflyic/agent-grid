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
    management_loop_interval_seconds: int = 3600  # 1 hour

    # Agent execution
    agent_bypass_permissions: bool = (
        True  # Use bypassPermissions mode for autonomous agents (required for non-interactive execution)
    )

    # Testing overrides (for development/testing only)
    test_force_planning_only: bool = False  # Force agents to only create subissues, not write code

    # Target repository
    target_repo: str = ""  # e.g. "myorg/myrepo"

    # Execution backend: "oz" (Warp Oz) or "fly" (Fly Machines)
    execution_backend: Literal["oz", "fly"] = "oz"

    # Warp Oz configuration
    warp_api_key: str = ""
    oz_environment_id: str = ""
    oz_model_id: str = "claude-sonnet-4-5-20250929"
    oz_poll_interval_seconds: int = 30  # How often to poll Oz for run completion

    # Fly.io configuration (used when execution_backend="fly")
    fly_api_token: str = ""
    fly_app_name: str = ""
    fly_worker_image: str = ""
    fly_worker_cpus: int = 2
    fly_worker_memory_mb: int = 4096
    fly_worker_region: str = "iad"
    coordinator_url: str = ""  # Public URL where workers can reach the coordinator API

    # Anthropic API (for classification/planning)
    anthropic_api_key: str = ""
    classification_model: str = "claude-sonnet-4-5-20250929"
    planning_model: str = "claude-sonnet-4-5-20250929"

    # Cost controls
    max_tokens_per_run: int = 100000
    max_cost_per_day_usd: float = 50.0
    max_retries_per_issue: int = 2
    max_ci_fix_retries: int = 5

    # Quality gate — confidence check before launching agents
    quality_gate_enabled: bool = True
    quality_gate_model: str = "claude-sonnet-4-5-20250929"

    # Proactive scanner — pick up unlabeled issues the agent is confident about
    proactive_scan_enabled: bool = False  # Off by default, opt-in
    proactive_scan_every_n_cycles: int = 12  # Every 12th cycle (~12h with 1h loop)
    proactive_max_per_cycle: int = 3  # Max issues to pick up per proactive scan
    proactive_min_score: int = 9  # Minimum confidence score (1-10) for proactive pickup

    # Dry-run mode — reads from GitHub but logs all writes to file instead
    dry_run: bool = False
    dry_run_output_file: str = "dry_run_output.jsonl"

    # GitHub Projects v2 integration
    github_project_number: int | None = None
    github_project_owner: str = ""  # org or user login owning the project
    github_project_label_status_map: str = '{"ag/todo": "Todo", "ag/in-progress": "In Progress", "ag/planning": "In Progress", "ag/review-pending": "In Review", "ag/blocked": "Blocked", "ag/done": "Done", "ag/failed": "Done"}'

    # Deployment mode
    deployment_mode: Literal["local", "coordinator"] = "local"

    model_config = {"env_prefix": "AGENT_GRID_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
