"""Orchestrates the AI-driven hotfix workflow for GitHub."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from dotenv import load_dotenv

from .github_auth import AppAuth
from .github_codebase import GitHubCodebase

logger = logging.getLogger(__name__)


class HotfixError(RuntimeError):
    """Raised for recoverable workflow failures."""


@dataclass(frozen=True)
class HotfixConfig:
    owner: str
    repo: str
    base_branch: str
    remote_name: str
    clone_url: str
    workspace_path: Optional[Path]

    @staticmethod
    def from_env(*, repo_path: Optional[Path] = None) -> "HotfixConfig":
        load_dotenv()
        owner = os.getenv("GITHUB_REPO_OWNER") or os.getenv("GITHUB_ORG") or ""
        repo = os.getenv("GITHUB_REPO_NAME") or os.getenv("GITHUB_REPO") or ""
        if not owner or not repo:
            raise HotfixError(
                "Set GITHUB_REPO_OWNER (or GITHUB_ORG) and GITHUB_REPO_NAME (or GITHUB_REPO) in your .env file."
            )
        base_branch = os.getenv("GITHUB_BASE_BRANCH", "main")
        remote_name = os.getenv("GITHUB_REMOTE_NAME", "origin")
        clone_url = os.getenv("GITHUB_REPO_CLONE_URL") or f"https://github.com/{owner}/{repo}.git"
        workspace_override = repo_path or os.getenv("HOTFIX_LOCAL_REPO")
        workspace_path = Path(workspace_override).expanduser().resolve() if workspace_override else None
        return HotfixConfig(
            owner=owner,
            repo=repo,
            base_branch=base_branch,
            remote_name=remote_name,
            clone_url=clone_url,
            workspace_path=workspace_path,
        )


def _slug_branch(reason: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", reason.strip().lower()).strip("-")
    if not cleaned:
        cleaned = "hotfix"
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"dengji/{cleaned[:40]}-{ts}"


def _short_title(reason: str) -> str:
    reason = reason.strip() or "AI Hotfix"
    title = reason[0].upper() + reason[1:]
    return title if len(title) <= 72 else f"{title[:69].rstrip()}..."


def _format_description(*, error_details: str, fix_details: str, reasoning: str, summary: str) -> str:
    def block(header: str, content: str) -> str:
        body = content.strip() or "N/A"
        return f"## {header}\n{body}"

    sections = [
        block("Error Details", error_details),
        block("Fix Details", fix_details),
        block("Reasoning", reasoning),
        block("Change Summary", summary),
    ]
    return "\n\n".join(sections)


class HotfixWorkflow:
    """
    Keeps a shallow mirror of the base branch, lets an AI mutate files locally,
    and publishes the edits via the GitHub App integration.
    """

    def __init__(self, config: HotfixConfig):
        self.config = config
        self._repo_path: Optional[Path] = None
        self._cleanup_callback: Optional[Callable[[], None]] = None

    # ---------- workspace helpers ----------
    def _set_workspace(self, path: Path, cleanup: Optional[Callable[[], None]]) -> None:
        self._repo_path = path
        self._cleanup_callback = cleanup

    def _clear_workspace(self) -> None:
        self._repo_path = None
        if self._cleanup_callback:
            self._cleanup_callback()
        self._cleanup_callback = None

    def _require_repo_path(self) -> Path:
        if not self._repo_path:
            raise HotfixError("Workspace not initialized.")
        return self._repo_path

    def _ensure_workspace(self, repo_path: Optional[Path]) -> None:
        if repo_path:
            repo_path = repo_path.expanduser().resolve()
            repo_path.mkdir(parents=True, exist_ok=True)
            self._set_workspace(repo_path, None)
            return
        tmp = tempfile.TemporaryDirectory(prefix="dengi-hotfix-")
        self._set_workspace(Path(tmp.name), tmp.cleanup)

    def _clone_base_repo(self) -> None:
        repo_path = self._require_repo_path()
        if repo_path.exists() and any(repo_path.iterdir()):
            # If the caller asked for a persistent repo path, make sure they gave us a git repo.
            if not (repo_path / ".git").exists():
                raise HotfixError(
                    f"Workspace {repo_path} is not empty and does not contain a Git repository."
                )
            return
        cmd = [
            "git",
            "clone",
            "--depth=1",
            "--branch",
            self.config.base_branch,
            self.config.clone_url,
            str(repo_path),
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "git clone failed"
            raise HotfixError(f"Unable to clone repo from {self.config.clone_url}: {detail}")
        if self.config.remote_name != "origin":
            self._git(["remote", "rename", "origin", self.config.remote_name])

    def _ensure_repo_ready(self) -> None:
        repo_path = self._require_repo_path()
        if not (repo_path / ".git").exists():
            self._clone_base_repo()
        self.sync_to_remote_main()

    # ---------- git helpers ----------
    def _git(self, args: Iterable[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *args]
        repo_path = self._require_repo_path()
        proc = subprocess.run(
            cmd,
            cwd=repo_path,
            text=True,
            capture_output=capture_output,
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
            raise HotfixError(f"Git command failed ({' '.join(cmd)}): {detail}")
        return proc

    def sync_to_remote_main(self) -> None:
        logger.info(
            "Syncing %s with %s/%s",
            self._require_repo_path(),
            self.config.remote_name,
            self.config.base_branch,
        )
        self._git(["fetch", "--prune", "--depth=1", self.config.remote_name, self.config.base_branch])
        self._git(["checkout", self.config.base_branch])
        self._git(["reset", "--hard", f"{self.config.remote_name}/{self.config.base_branch}"])
        self._git(["clean", "-fdx"])

    def checkout_branch(self, branch_name: str) -> None:
        logger.info("Checking out local branch %s", branch_name)
        self._git(["checkout", "-B", branch_name])

    def cleanup_branch(self, branch_name: str) -> None:
        logger.info("Resetting workspace back to %s", self.config.base_branch)
        self._git(["checkout", self.config.base_branch])
        try:
            self._git(["branch", "-D", branch_name])
        except HotfixError:
            pass
        self.sync_to_remote_main()

    def stage_changes(self) -> None:
        self._git(["add", "-A"])

    def staged_files(self) -> list[str]:
        proc = self._git(["diff", "--cached", "--name-only"], capture_output=True)
        return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]

    def payloads_from_files(self, files: Iterable[str]) -> Dict[str, bytes]:
        payloads: Dict[str, bytes] = {}
        repo_path = self._require_repo_path()
        for relative in files:
            file_path = repo_path / relative
            if not file_path.exists():
                raise HotfixError(
                    f"Cannot include deleted file {relative}; deletions are not yet supported by commit_files."
                )
            payloads[relative] = file_path.read_bytes()
        return payloads

    # ---------- workflow ----------
    def execute(
        self,
        *,
        reason_for_fix: str,
        error_details: str,
        fix_details: str,
        reasoning: str,
        apply_changes: Callable[[Path], str],
        repo_path: Optional[Path] = None,
    ) -> dict:
        branch_name = _slug_branch(reason_for_fix)
        title = _short_title(reason_for_fix)

        workspace_override = repo_path or self.config.workspace_path
        self._ensure_workspace(workspace_override)
        repo_root = self._require_repo_path()
        logger.info("Using hotfix workspace at %s", repo_root)
        try:
            self._ensure_repo_ready()
            self.checkout_branch(branch_name)

            logger.info("Invoking AI change callback for branch %s", branch_name)
            summary = apply_changes(repo_root)
            if summary is None:
                summary = ""

            self.stage_changes()
            files = self.staged_files()
            if not files:
                self.cleanup_branch(branch_name)
                raise HotfixError("AI callback did not produce any modifications.")

            payloads = self.payloads_from_files(files)
            body = _format_description(
                error_details=error_details,
                fix_details=fix_details,
                reasoning=reasoning,
                summary=summary,
            )

            logger.info("Pushing branch %s to %s/%s via GitHub API", branch_name, self.config.owner, self.config.repo)
            auth = AppAuth(
                app_id=os.environ["GITHUB_APP_ID"],
                installation_id=os.environ["GITHUB_APP_INSTALLATION_ID"],
                private_key_pem=os.environ["GITHUB_APP_PRIVATE_KEY_PEM"],
            )
            gh = GitHubCodebase(self.config.owner, self.config.repo, auth)
            gh.create_branch(new_branch=branch_name, base_ref=self.config.base_branch)
            gh.commit_files(branch=branch_name, message=title, files=payloads)
            pr_number = gh.open_pr(
                head_branch=branch_name,
                base_branch=self.config.base_branch,
                title=title,
                body=body,
            )

            self.cleanup_branch(branch_name)
            logger.info("Hotfix PR created: #%s", pr_number)
            return {
                "branch": branch_name,
                "title": title,
                "description": body,
                "pr_number": pr_number,
                "changed_files": files,
            }
        finally:
            self._clear_workspace()


def run_hotfix_workflow(
    *,
    reason_for_fix: str,
    error_details: str,
    fix_details: str,
    reasoning: str,
    apply_changes: Callable[[Path], str],
    repo_path: Optional[Path] = None,
) -> dict:
    """
    Convenience entry point. Pass a callback that mutates the checkout and returns a summary string.
    """
    config = HotfixConfig.from_env(repo_path=repo_path)
    workflow = HotfixWorkflow(config)
    return workflow.execute(
        reason_for_fix=reason_for_fix,
        error_details=error_details,
        fix_details=fix_details,
        reasoning=reasoning,
        apply_changes=apply_changes,
        repo_path=repo_path or config.workspace_path,
    )
