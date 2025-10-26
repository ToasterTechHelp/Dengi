"""Continuous pipeline that turns log events into GitHub hotfix PRs."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, Optional

from dotenv import load_dotenv

from ..agents.runAgents import run_agents
from ..github.hotfix_workflow import HotfixError, run_hotfix_workflow
from ..monitor.service import LogEvent

logger = logging.getLogger(__name__)


def _looks_like_diff(content: str) -> bool:
    sample = content.lstrip()
    return sample.startswith(("diff ", "---", "+++")) or "\n@@ " in content


def _apply_git_patch(repo_path: Path, patch_text: str) -> None:
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        input=patch_text,
        text=True,
        capture_output=True,
        cwd=repo_path,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "git apply failed"
        raise HotfixError(f"Unable to apply AI patch plan via git apply: {detail}")


def _write_full_file(repo_path: Path, relative_path: str, content: str) -> None:
    file_path = (repo_path / relative_path).resolve()
    repo_root = repo_path.resolve()
    if not str(file_path).startswith(str(repo_root)):
        raise HotfixError(f"Refusing to write outside repo: {relative_path}")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def apply_patch_plan(repo_path: Path, patch_plan: Dict) -> str:
    """
    Applies the structured patch JSON returned by the orchestrator.
    """
    if not patch_plan:
        raise HotfixError("Patch plan is empty.")

    applied_any = False
    top_level_patch = patch_plan.get("patch")
    if isinstance(top_level_patch, str) and top_level_patch.strip():
        _apply_git_patch(repo_path, top_level_patch)
        applied_any = True

    for entry in patch_plan.get("files", []) or []:
        path = entry.get("path")
        patch_text = entry.get("patch")
        if not path or not isinstance(patch_text, str) or not patch_text.strip():
            continue
        if _looks_like_diff(patch_text):
            _apply_git_patch(repo_path, patch_text)
        else:
            _write_full_file(repo_path, path, patch_text)
        applied_any = True

    if not applied_any:
        raise HotfixError("Patch plan did not contain any usable patch content.")

    return patch_plan.get("summary") or "Applied AI patch plan."


def _format_fix_details(patch_plan: Dict) -> str:
    summary = patch_plan.get("summary") or "AI-generated fix"
    lines = [summary]
    for entry in patch_plan.get("files", []) or []:
        path = entry.get("path")
        if not path:
            continue
        preview = entry.get("patch", "")
        preview_snippet = preview.strip().splitlines()
        snippet = preview_snippet[0][:120] if preview_snippet else ""
        extra = f" – {snippet}" if snippet else ""
        lines.append(f"- {path}{extra}")
    notes = patch_plan.get("notes")
    if notes:
        lines.append(f"Notes: {notes}")
    return "\n".join(lines)


def _format_error_details(event: LogEvent) -> str:
    parts = [
        f"Container: {event.container_name} ({event.container_id[:12]})",
        f"Stream: {event.stream}",
        f"Timestamp: {event.timestamp.isoformat()}",
        f"Message: {event.text}",
    ]
    if event.stacktrace:
        parts.append("Stacktrace:\n" + event.stacktrace)
    return "\n".join(parts)


class ContinuousHotfixPipeline:
    """
    Consumes LogEvent inputs, calls the LLM orchestrator, and opens GitHub PRs via the hotfix workflow.
    """

    def __init__(
        self,
        *,
        repo_path: Optional[Path] = None,
        dedupe_window: int = 32,
        fail_fast: bool = False,
        on_fatal: Optional[Callable[[BaseException], None]] = None,
    ):
        load_dotenv()
        self.repo_path = repo_path
        self.recent_errors: Deque[str] = deque(maxlen=dedupe_window)
        self.lock = threading.Lock()
        self.fail_fast = fail_fast
        self.on_fatal = on_fatal

    def handle_event(self, event: LogEvent) -> None:
        if not event.is_error:
            return
        signature = json.dumps(
            {
                "container": event.container_name,
                "message": event.text,
                "stack": event.stacktrace or "",
            },
            sort_keys=True,
        )
        if signature in self.recent_errors:
            logger.info("Skipping duplicate error event from %s", event.container_name)
            return
        self.recent_errors.append(signature)
        threading.Thread(target=self._process_event, args=(event,), daemon=True).start()

    def _process_event(self, event: LogEvent) -> None:
        with self.lock:
            try:
                agent_payload = run_agents(self._format_agent_prompt(event))
            except Exception as exc:
                logger.exception("Failed to run LLM orchestrator for %s", event.container_name)
                self._handle_failure(exc)
                return

            if not isinstance(agent_payload, dict):
                logger.warning("Agent payload was not a dict: %r", agent_payload)
                return

            patch_plan = agent_payload.get("patch_plan") or {}
            if not patch_plan:
                logger.warning("No patch plan returned for %s", event.container_name)
                return

            reason = patch_plan.get("summary") or event.text[:80]
            error_details = _format_error_details(event)
            fix_details = _format_fix_details(patch_plan)
            reasoning = patch_plan.get("notes") or f"Proposed by {agent_payload.get('chosen_agent', 'backend')} specialist."

            def _apply(repo_path: Path) -> str:
                return apply_patch_plan(repo_path, patch_plan)

            try:
                result = run_hotfix_workflow(
                    reason_for_fix=reason,
                    error_details=error_details,
                    fix_details=fix_details,
                    reasoning=reasoning,
                    apply_changes=_apply,
                    repo_path=self.repo_path,
                )
            except HotfixError as exc:
                logger.exception("Hotfix workflow failed for %s", event.container_name)
                self._handle_failure(exc)
                return

            logger.info(
                "Opened PR #%s (%s) touching %d files",
                result["pr_number"],
                result["branch"],
                len(result["changed_files"]),
            )

    def _handle_failure(self, exc: BaseException) -> None:
        if not self.fail_fast:
            return
        if self.on_fatal:
            self.on_fatal(exc)
        else:
            raise exc

    @staticmethod
    def _format_agent_prompt(event: LogEvent) -> str:
        if event.stacktrace:
            return f"{event.text}\n\nStacktrace:\n{event.stacktrace}"
        return event.text
