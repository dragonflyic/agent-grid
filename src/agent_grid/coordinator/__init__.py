"""Coordinator module for agent orchestration."""

from .public_api import (
    # Models
    NudgeRequest,
    NudgeRequestCreate,
    utc_now,
    # Router
    coordinator_router,
)
from .scheduler import Scheduler, get_scheduler
from .nudge_handler import NudgeHandler, get_nudge_handler
from .budget_manager import BudgetManager, get_budget_manager
from .management_loop import ManagementLoop, get_management_loop
from .database import Database, get_database
from .chat_logger import AgentEventLogger, get_agent_event_logger
from .scanner import Scanner, get_scanner
from .classifier import Classifier, get_classifier
from .planner import Planner, get_planner
from .prompt_builder import build_prompt
from .pr_monitor import PRMonitor, get_pr_monitor
from .blocker_resolver import BlockerResolver, get_blocker_resolver
from .dependency_resolver import DependencyResolver, get_dependency_resolver

__all__ = [
    # Public API - Models
    "NudgeRequest",
    "NudgeRequestCreate",
    "utc_now",
    # Public API - Router
    "coordinator_router",
    # Services
    "Scheduler",
    "get_scheduler",
    "NudgeHandler",
    "get_nudge_handler",
    "BudgetManager",
    "get_budget_manager",
    "ManagementLoop",
    "get_management_loop",
    "Database",
    "get_database",
    "AgentEventLogger",
    "get_agent_event_logger",
    # Tech Lead pipeline
    "Scanner",
    "get_scanner",
    "Classifier",
    "get_classifier",
    "Planner",
    "get_planner",
    "build_prompt",
    "PRMonitor",
    "get_pr_monitor",
    "BlockerResolver",
    "get_blocker_resolver",
    "DependencyResolver",
    "get_dependency_resolver",
]
