"""Continuous pipeline that turns log events into GitHub hotfix PRs."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from dotenv import load_dotenv

from ..agents.runAgents import run_agents
from ..github import AppAuth, GitHubCodebase
from ..github.hotfix_workflow import HotfixConfig, HotfixError, run_hotfix_workflow
from ..monitor.service import LogEvent

logger = logging.getLogger(__name__)


def _looks_like_diff(content: str) -> bool:
    sample = content.lstrip()
    return sample.startswith(("diff ", "---", "+++")) or "\n@@ " in content


def _git_ls_files(repo_path: Path) -> Dict[str, str]:
    """
    Returns a lowercase -> actual path mapping for all tracked files.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git wrapper
        raise HotfixError("git is required inside the hotfix workspace") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "git ls-files failed"
        raise HotfixError(detail)
    mapping: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        mapping[line.lower()] = line
    return mapping


def _normalize_relative_path(relative_path: str, case_map: Dict[str, str]) -> str:
    normalized = relative_path.replace("\\", "/").lstrip("./")
    return case_map.get(normalized.lower(), normalized)


def _normalize_patch_paths(patch_text: str, case_map: Dict[str, str]) -> str:
    def _rewrite_label(label: str) -> str:
        label = label.strip()
        if not label or label.endswith("/dev/null"):
            return label

        # Handle quoted paths from git diff
        if label.startswith('"') and label.endswith('"'):
            is_quoted = True
            label_content = label[1:-1]
        else:
            is_quoted = False
            label_content = label

        if label_content.startswith(("a/", "b/")):
            prefix, _, remainder = label_content.partition("/")
            resolved = _normalize_relative_path(remainder, case_map)
            new_label_content = f"{prefix}/{resolved}"
        else:
            new_label_content = label_content

        if is_quoted:
            return f'"{new_label_content}"'
        return new_label_content

    def _rewrite_line(line: str) -> str:
        if line.startswith("diff --git "):
            # This is a fragile but simple way to handle quoted paths
            # A proper parser would be better, but this should work for now
            if ' "' in line and line.endswith('"'):
                try:
                    first_part, a_path, b_path = line.rsplit(' "', 2)
                    a_path = '"' + a_path
                    b_path = '"' + b_path
                    parts = [first_part, a_path, b_path]
                except ValueError:
                    parts = line.strip().split() # fallback
            else:
                parts = line.strip().split()

            if len(parts) >= 3: # diff --git a/path b/path
                parts[-2] = _rewrite_label(parts[-2])
                parts[-1] = _rewrite_label(parts[-1])
            return " ".join(parts)
        if line.startswith(("--- ", "+++ ")):
            prefix = line[:4]
            rest = line[4:]
            if rest.strip() == "/dev/null":
                return line
            return f"{prefix}{_rewrite_label(rest)}"
        return line

    normalized_lines = []
    for raw_line in patch_text.splitlines(keepends=True):
        if raw_line.endswith("\r\n"):
            body, newline = raw_line[:-2], "\r\n"
        elif raw_line.endswith("\n") or raw_line.endswith("\r"):
            body, newline = raw_line[:-1], raw_line[-1]
        else:
            body, newline = raw_line, ""
        normalized_lines.append(_rewrite_line(body) + newline)
    
    result = "".join(normalized_lines)
    
    # Ensure patch ends with a newline (git apply expects this)
    if result and not result.endswith('\n'):
        result += '\n'
    
    return result


def _validate_patch_format(patch_text: str) -> tuple[bool, str]:
    """
    Validates basic patch format and returns (is_valid, error_message).
    """
    if not patch_text or not patch_text.strip():
        return False, "Empty patch"
    
    lines = patch_text.splitlines()
    
    # Check for basic diff structure
    has_diff_header = False
    has_file_markers = False
    has_hunk_header = False
    
    for i, line in enumerate(lines, 1):
        if line.startswith("diff --git "):
            has_diff_header = True
        elif line.startswith(("--- ", "+++ ")):
            has_file_markers = True
        elif line.startswith("@@ "):
            # Validate hunk header format: @@ -X,Y +A,B @@
            if not re.match(r'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@', line):
                return False, f"Invalid hunk header at line {i}: {line[:50]}"
            has_hunk_header = True
    
    if not has_diff_header and not has_file_markers:
        return False, "Missing diff headers (diff --git, ---, +++)"
    
    if has_file_markers and not has_hunk_header:
        return False, "Missing hunk headers (@@ ... @@)"
    
    return True, ""


def _apply_git_patch(repo_path: Path, patch_text: str, case_map: Dict[str, str]) -> None:
    # Validate patch format before attempting to apply
    is_valid, error_msg = _validate_patch_format(patch_text)
    if not is_valid:
        logger.error("Patch validation failed: %s", error_msg)
        logger.error("Invalid patch content:\n%s", patch_text[:1000])
        raise HotfixError(f"Patch validation failed: {error_msg}")
    
    patch_body = _normalize_patch_paths(patch_text, case_map)
    
    # Debug: Log the patch to help diagnose issues
    logger.debug("Attempting to apply patch:\n%s", patch_body[:2000])  # First 2000 chars
    
    # Try different apply strategies
    strategies = [
        ["git", "apply", "--whitespace=nowarn", "-"],
        ["git", "apply", "--whitespace=nowarn", "--ignore-whitespace", "-"],
        ["git", "apply", "--whitespace=nowarn", "--unidiff-zero", "-"],
        ["git", "apply", "--whitespace=nowarn", "--ignore-whitespace", "--unidiff-zero", "-"],
        ["git", "apply", "--whitespace=nowarn", "--3way", "-"],
        ["git", "apply", "--whitespace=nowarn", "--reject", "-"],  # Creates .rej files for failed hunks
    ]
    
    last_error = None
    for strategy in strategies:
        proc = subprocess.run(
            strategy,
            input=patch_body,
            text=True,
            capture_output=True,
            cwd=repo_path,
        )
        if proc.returncode == 0:
            logger.info("Successfully applied patch using strategy: %s", " ".join(strategy[1:-1]))
            return
        last_error = proc.stderr.strip() or proc.stdout.strip() or "git apply failed"
        logger.debug("Strategy %s failed: %s", " ".join(strategy[1:-1]), last_error)
    
    # All strategies failed - log detailed error
    detail = last_error or "git apply failed with all strategies"
    logger.error("Failed patch content:\n%s", patch_body)
    logger.error("Git apply error: %s", detail)
    
    raise HotfixError(f"Unable to apply AI patch plan via git apply: {detail}")


def _write_full_file(repo_path: Path, relative_path: str, content: str, case_map: Dict[str, str]) -> None:
    resolved_relative = _normalize_relative_path(relative_path, case_map)
    file_path = (repo_path / resolved_relative).resolve()
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

    # Log the patch plan structure for debugging
    logger.info("Received patch plan: %s", json.dumps(patch_plan, indent=2)[:1000])

    applied_any = False
    case_map = _git_ls_files(repo_path)
    top_level_patch = patch_plan.get("patch")
    if isinstance(top_level_patch, str) and top_level_patch.strip():
        logger.info("Applying top-level patch (%d chars)", len(top_level_patch))
        _apply_git_patch(repo_path, top_level_patch, case_map)
        applied_any = True

    files_list = patch_plan.get("files", []) or []
    logger.info("Processing %d file entries from patch plan", len(files_list))
    
    for i, entry in enumerate(files_list):
        path = entry.get("path")
        patch_text = entry.get("patch")
        
        if not path:
            logger.warning("File entry %d missing 'path' field: %s", i, entry)
            continue
            
        if not isinstance(patch_text, str) or not patch_text.strip():
            logger.warning("File entry %d (%s) has empty or invalid patch", i, path)
            continue
            
        logger.info("Processing file %d: %s (%d chars)", i, path, len(patch_text))
        
        if _looks_like_diff(patch_text):
            logger.info("Detected diff format for %s", path)
            _apply_git_patch(repo_path, patch_text, case_map)
        else:
            logger.info("Detected full file replacement for %s", path)
            _write_full_file(repo_path, path, patch_text, case_map)
        applied_any = True

    if not applied_any:
        logger.error("No usable patch content found. Patch plan keys: %s", list(patch_plan.keys()))
        logger.error("Full patch plan: %s", json.dumps(patch_plan, indent=2))
        logger.error("Top-level patch type: %s, empty: %s", 
                    type(top_level_patch).__name__, 
                    not (isinstance(top_level_patch, str) and top_level_patch.strip()))
        logger.error("Files list length: %d", len(files_list))
        
        # Check if the AI provided analysis but no patch
        summary = patch_plan.get("summary", "")
        notes = patch_plan.get("notes", "")
        if summary or notes:
            raise HotfixError(
                f"AI provided analysis but no actionable patch. "
                f"Summary: {summary[:100]}. Notes: {notes[:100]}"
            )
        
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
        dedupe_ttl: float = 600.0,
        fail_fast: bool = False,
        on_fatal: Optional[Callable[[BaseException], None]] = None,
        repo_tree_depth: int = 2,
    ):
        load_dotenv()
        self.repo_path = repo_path
        ttl_override = os.getenv("DENGI_EVENT_TTL_SECONDS")
        if ttl_override:
            try:
                dedupe_ttl = float(ttl_override)
            except ValueError:
                logger.warning("Invalid DENGI_EVENT_TTL_SECONDS=%r; defaulting to %s", ttl_override, dedupe_ttl)
        self.dedupe_ttl = max(30.0, dedupe_ttl)
        self.dedupe_window = max(1, dedupe_window)
        self.recent_errors: Dict[str, float] = {}
        self.recent_prs: Dict[str, float] = {}
        self.dedupe_lock = threading.Lock()
        self.lock = threading.Lock()
        self.fail_fast = fail_fast
        self.on_fatal = on_fatal
        self.failed = False
        self.repo_tree_depth = repo_tree_depth
        self._repo_tree_cache: Optional[str] = None
        self._repo_tree_lock = threading.Lock()
        self._hotfix_config: Optional[HotfixConfig] = None

    def handle_event(self, event: LogEvent) -> None:
        if not event.is_error:
            return
        if self.fail_fast and self.failed:
            logger.info("Fail-fast is enabled; ignoring event from %s after fatal error.", event.container_name)
            return
        signature = self._event_signature(event)
        now = time.monotonic()
        with self.dedupe_lock:
            self._purge_expired(now)
            if self._is_recent(self.recent_errors, signature, now):
                logger.info("Skipping duplicate error event from %s (already processed recently)", event.container_name)
                return
            if self._is_recent(self.recent_prs, signature, now):
                logger.info("Skipping %s because a PR already exists for this error.", event.container_name)
                return
            self.recent_errors[signature] = now
        threading.Thread(target=self._process_event, args=(event, signature), daemon=True).start()

    def _process_event(self, event: LogEvent, signature: str) -> None:
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
            if not self._has_usable_patch_content(patch_plan):
                logger.warning(
                    "Patch plan for %s did not contain actionable edits. Summary=%s Notes=%s",
                    event.container_name,
                    patch_plan.get("summary"),
                    patch_plan.get("notes"),
                )
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
                if "Unable to apply AI patch plan via git apply" in str(exc):
                    logger.warning(
                        "Patch plan could not be applied for %s: %s",
                        event.container_name,
                        exc,
                    )
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Patch plan payload: %s", json.dumps(patch_plan, indent=2)[:4000])
                    return
                logger.exception("Hotfix workflow failed for %s", event.container_name)
                self._handle_failure(exc)
                return

            logger.info(
                "Opened PR #%s (%s) touching %d files",
                result["pr_number"],
                result["branch"],
                len(result["changed_files"]),
            )
            with self.dedupe_lock:
                self.recent_prs[signature] = time.monotonic()

    def _handle_failure(self, exc: BaseException) -> None:
        if not self.fail_fast:
            return
        self.failed = True
        if self.on_fatal:
            self.on_fatal(exc)
        else:
            raise exc

    @staticmethod
    def _format_log_with_stack(event: LogEvent) -> str:
        if event.stacktrace:
            return f"{event.text}\n\nStacktrace:\n{event.stacktrace}"
        return event.text

    def _format_agent_prompt(self, event: LogEvent) -> str:
        log_block = self._format_log_with_stack(event)
        tree = self._get_repo_tree_snapshot()
        file_contents = self._get_relevant_file_contents(event)
        
        parts = [log_block]
        if tree:
            parts.append(f"\n[REPOSITORY TREE]\n{tree}")
        if file_contents:
            parts.append(f"\n[RELEVANT FILE CONTENTS]\n{file_contents}")
        
        return "\n".join(parts)

    def _get_relevant_file_contents(self, event: LogEvent) -> str:
        """Extract file paths from error and fetch their contents from GitHub."""
        try:
            config = self._hotfix_config or HotfixConfig.from_env(repo_path=self.repo_path)
            self._hotfix_config = config
            auth = AppAuth(
                app_id=os.environ["GITHUB_APP_ID"],
                installation_id=os.environ["GITHUB_APP_INSTALLATION_ID"],
                private_key_pem=os.environ["GITHUB_APP_PRIVATE_KEY_PEM"],
            )
            gh = GitHubCodebase(config.owner, config.repo, auth)
            
            # Extract potential file paths from error message and stacktrace
            file_paths = self._extract_file_paths(event)
            
            # If no paths found, try common files based on container name
            if not file_paths:
                file_paths = self._get_common_files_for_container(event.container_name)
                logger.info("No file paths in error, using common files for %s: %s", 
                           event.container_name, file_paths)

            file_paths = self._ensure_dual_stack_context(file_paths, event)
            # If still nothing (e.g., backend container emitting frontend errors),
            # fall back to a cross-service default set so agents can inspect likely files.
            if not file_paths:
                file_paths = self._get_cross_service_defaults()
                logger.info("Falling back to cross-service defaults: %s", file_paths)
            else:
                logger.info("Extracted file paths from error: %s", file_paths)
            
            if not file_paths:
                logger.warning("No file paths to fetch for context")
                return ""
            
            contents = []
            for file_path in file_paths[:5]:  # Limit to 5 files to avoid token overflow
                try:
                    content = gh.read_file(file_path, ref=config.base_branch, as_text=True)
                    contents.append(f"=== {file_path} ===\n{content}\n")
                    logger.info("Fetched file content for AI context: %s (%d chars)", file_path, len(content))
                except Exception as e:
                    logger.debug("Could not fetch file %s: %s", file_path, e)
                    continue
            
            if not contents:
                logger.warning("Could not fetch any file contents")
            
            return "\n".join(contents)
        except Exception:
            logger.exception("Failed to fetch relevant file contents")
            return ""

    def _event_signature(self, event: LogEvent) -> str:
        return json.dumps(
            {
                "container": event.container_name.lower(),
                "message": self._normalize_text(event.text, limit=200),
                "stack": self._normalize_text(event.stacktrace, limit=160) if event.stacktrace else "",
            },
            sort_keys=True,
        )

    @staticmethod
    def _normalize_text(text: str, *, limit: int) -> str:
        cleaned = re.sub(r"\s+", " ", text.strip().lower())
        cleaned = re.sub(r"\d+", "<num>", cleaned)
        return cleaned[:limit]

    def _is_recent(self, store: Dict[str, float], signature: str, now: float) -> bool:
        ts = store.get(signature)
        if ts is None:
            return False
        return (now - ts) < self.dedupe_ttl

    def _purge_expired(self, now: float) -> None:
        cutoff = now - self.dedupe_ttl
        for store in (self.recent_errors, self.recent_prs):
            expired = [key for key, ts in store.items() if ts < cutoff]
            for key in expired:
                store.pop(key, None)
            self._trim_store(store)

    def _trim_store(self, store: Dict[str, float]) -> None:
        if len(store) <= self.dedupe_window:
            return
        excess = len(store) - self.dedupe_window
        for key, _ in sorted(store.items(), key=lambda kv: kv[1]):
            store.pop(key, None)
            excess -= 1
            if excess <= 0:
                break

    def _get_common_files_for_container(self, container_name: str) -> list[str]:
        """Return common files to check based on container name."""
        container_lower = container_name.lower()
        
        # Map container names to common files
        if "backend" in container_lower or "server" in container_lower or "api" in container_lower:
            return ["backend/server.js", "backend/index.js", "backend/app.js", "backend/package.json"]
        elif "frontend" in container_lower or "client" in container_lower or "web" in container_lower:
            return ["frontend/src/App.js", "frontend/src/index.js", "frontend/package.json"]
        elif "database" in container_lower or "db" in container_lower:
            return ["backend/db.js", "backend/models/index.js"]
        
        # Default: try to find main entry points
        return ["server.js", "index.js", "app.js", "main.py"]

    def _get_cross_service_defaults(self) -> list[str]:
        """Fallback files when no context is available, mixing backend and frontend hot spots."""
        return [
            "backend/server.js",
            "backend/app.js",
            "frontend/src/App.js",
            "frontend/src/index.js",
        ]

    def _ensure_dual_stack_context(self, file_paths: list[str], event: LogEvent) -> list[str]:
        """Make sure both backend and frontend hotspots are available to the LLM."""
        normalized = {path.lower() for path in file_paths}

        # Always include the cross-service defaults so both stacks are visible.
        for candidate in self._get_cross_service_defaults():
            if candidate.lower() not in normalized:
                file_paths.append(candidate)
                normalized.add(candidate.lower())

        # If the error message explicitly references frontend terminology but we still lack frontend files,
        # double-check by appending the standard frontend set.
        text_blob = f"{event.text} {event.stacktrace or ''}".lower()
        if any(keyword in text_blob for keyword in ("frontend", "client", "react", "browser", "component")):
            for candidate in self._get_common_files_for_container("frontend"):
                if candidate.lower() not in normalized:
                    file_paths.append(candidate)
                    normalized.add(candidate.lower())

        return file_paths

    @staticmethod
    def _has_usable_patch_content(patch_plan: Dict) -> bool:
        top_level = patch_plan.get("patch")
        if isinstance(top_level, str) and top_level.strip():
            return True
        for entry in patch_plan.get("files") or []:
            if isinstance(entry, dict) and isinstance(entry.get("patch"), str) and entry["patch"].strip():
                return True
        return False

    def _extract_file_paths(self, event: LogEvent) -> list[str]:
        """Extract potential file paths from error message and stacktrace."""
        import re
        
        text = event.text
        if event.stacktrace:
            text += "\n" + event.stacktrace
        
        # Common patterns for file paths in error messages
        patterns = [
            r'(?:File|at|in)\s+"?([a-zA-Z0-9_/.-]+\.[a-zA-Z]+)"?',  # File "path/to/file.py"
            r'([a-zA-Z0-9_/.-]+\.(?:js|py|ts|tsx|jsx|java|go|rs|rb))',  # Direct file extensions
            r'(?:from|import)\s+["\']([a-zA-Z0-9_/.-]+)["\']',  # Import statements
        ]
        
        file_paths = set()
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                # Clean up the path
                path = match.strip().strip('"\'')
                # Remove line numbers like :123
                path = re.sub(r':\d+$', '', path)
                # Skip very short paths or paths with spaces (likely not real files)
                if len(path) > 3 and ' ' not in path and not path.startswith('/'):
                    file_paths.add(path)
        
        return list(file_paths)


    def _get_repo_tree_snapshot(self) -> str:
        with self._repo_tree_lock:
            if self._repo_tree_cache is not None:
                return self._repo_tree_cache
            try:
                config = self._hotfix_config or HotfixConfig.from_env(repo_path=self.repo_path)
                self._hotfix_config = config
                auth = AppAuth(
                    app_id=os.environ["GITHUB_APP_ID"],
                    installation_id=os.environ["GITHUB_APP_INSTALLATION_ID"],
                    private_key_pem=os.environ["GITHUB_APP_PRIVATE_KEY_PEM"],
                )
                gh = GitHubCodebase(config.owner, config.repo, auth)
                tree_text = gh.tree(
                    ref=config.base_branch,
                    recursive=True,
                    as_text_tree=True,
                    max_depth=self.repo_tree_depth,
                )
                if isinstance(tree_text, str):
                    self._repo_tree_cache = tree_text
                else:
                    self._repo_tree_cache = ""
            except Exception:
                logger.exception("Failed to load repository tree for agent context")
                self._repo_tree_cache = ""
            return self._repo_tree_cache or ""
