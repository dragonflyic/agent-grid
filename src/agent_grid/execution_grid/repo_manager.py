"""Repository management for agent executions."""

import asyncio
import shutil
from pathlib import Path
from uuid import UUID

from ..config import settings


class RepoManager:
    """
    Manages repository cloning and cleanup for agent executions.

    Each execution gets a fresh clone in a temp directory.
    """

    def __init__(self, base_path: str | None = None):
        self._base_path = Path(base_path or settings.repo_base_path)
        self._base_path.mkdir(parents=True, exist_ok=True)

    def get_execution_path(self, execution_id: UUID) -> Path:
        """Get the working directory path for an execution."""
        return self._base_path / str(execution_id)

    async def clone_repo(
        self,
        execution_id: UUID,
        repo_url: str,
        branch: str | None = None,
    ) -> Path:
        """
        Clone a repository for an execution.

        Args:
            execution_id: Unique ID for this execution.
            repo_url: URL of the repository to clone.
            branch: Optional branch to checkout.

        Returns:
            Path to the cloned repository.
        """
        work_dir = self.get_execution_path(execution_id)

        # Ensure clean directory
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

        # Clone the repository
        clone_cmd = ["git", "clone", "--depth", "1"]
        if branch:
            clone_cmd.extend(["--branch", branch])
        clone_cmd.extend([repo_url, str(work_dir)])

        process = await asyncio.create_subprocess_exec(
            *clone_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"Failed to clone repository: {stderr.decode()}")

        return work_dir

    async def create_branch(
        self,
        execution_id: UUID,
        branch_name: str,
    ) -> None:
        """
        Create and checkout a new branch for the agent's work.

        Args:
            execution_id: Execution ID.
            branch_name: Name of the branch to create.
        """
        work_dir = self.get_execution_path(execution_id)

        # Create and checkout branch
        process = await asyncio.create_subprocess_exec(
            "git", "checkout", "-b", branch_name,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"Failed to create branch: {stderr.decode()}")

    async def push_branch(
        self,
        execution_id: UUID,
        branch_name: str,
    ) -> None:
        """
        Push the agent's branch to the remote.

        Args:
            execution_id: Execution ID.
            branch_name: Name of the branch to push.
        """
        work_dir = self.get_execution_path(execution_id)

        process = await asyncio.create_subprocess_exec(
            "git", "push", "-u", "origin", branch_name,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"Failed to push branch: {stderr.decode()}")

    async def cleanup(self, execution_id: UUID) -> None:
        """
        Clean up the working directory for an execution.

        Args:
            execution_id: Execution ID to clean up.
        """
        work_dir = self.get_execution_path(execution_id)
        if work_dir.exists():
            shutil.rmtree(work_dir)

    async def cleanup_all(self) -> None:
        """Clean up all execution directories."""
        if self._base_path.exists():
            for child in self._base_path.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)


# Global instance
_repo_manager: RepoManager | None = None


def get_repo_manager() -> RepoManager:
    """Get the global repo manager instance."""
    global _repo_manager
    if _repo_manager is None:
        _repo_manager = RepoManager()
    return _repo_manager
