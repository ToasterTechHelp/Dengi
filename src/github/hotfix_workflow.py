"""Orchestrates the AI-driven hotfix workflow for GitHub."""
from __future__ import annotations

import logging
import os
import re
import subprocess
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
    repo_path: Path

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
        workspace = repo_path or Path(__file__).resolve().parents[2]
        return HotfixConfig(
            owner=owner,
            repo=repo,
            base_branch=base_branch,
            remote_name=remote_name,
            repo_path=workspace,
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

    # ---------- git helpers ----------
    def _git(self, args: Iterable[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *args]
        proc = subprocess.run(
            cmd,
            cwd=self.config.repo_path,
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
            self.config.repo_path,
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
        for relative in files:
            file_path = self.config.repo_path / relative
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
    ) -> dict:
        branch_name = _slug_branch(reason_for_fix)
        title = _short_title(reason_for_fix)

        self.sync_to_remote_main()
        self.checkout_branch(branch_name)

        logger.info("Invoking AI change callback for branch %s", branch_name)
        summary = apply_changes(self.config.repo_path)
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
    )
