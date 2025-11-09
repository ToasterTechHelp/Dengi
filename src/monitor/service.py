"""
Monitoring service that tails Docker container logs and runs them through a classifier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections import deque
import os
import threading
import time
from typing import Callable, Deque, Dict, Iterable, Optional

try:
    import docker
    from docker.client import DockerClient
    from docker.models.containers import Container
except ImportError:  # pragma: no cover - docker is optional for linting environments
    docker = None
    DockerClient = Container = None  # type: ignore

from .rule_engine import RuleBasedClassifier, RuleMatch
from .stacktrace import StackTraceExtractor
from ..agents.runAgents import run_agents


@dataclass
class LogEvent:
    """
    Structured representation of a single log line.
    """

    container_id: str
    container_name: str
    timestamp: datetime
    stream: str
    text: str
    is_error: bool
    rule_match: Optional[RuleMatch] = None
    stacktrace: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)


class DockerLogMonitor:
    """
    Supervises Docker containers, reads stdout/stderr/logs, classifies lines, and emits events.
    """

    def __init__(
        self,
        *,
        docker_client: Optional[DockerClient] = None,
        classifier: Optional[RuleBasedClassifier] = None,
        stacktrace_extractor: Optional[StackTraceExtractor] = None,
        poll_interval: float = 15.0,
        initial_delay: float = 10.0,
        history_size: int = 100,
        include: Optional[Iterable[str]] = None,
        exclude: Optional[Iterable[str]] = None,
        event_handler: Optional[Callable[[LogEvent], None]] = None,
        exclude_self: bool = True,
    ):
        if docker is None:
            raise RuntimeError("docker SDK is not installed. Add 'docker' to requirements.txt and install it.")
        self.client: DockerClient = docker_client or docker.from_env()
        self.classifier = classifier or RuleBasedClassifier()
        self.stacktrace_extractor = stacktrace_extractor or StackTraceExtractor()
        self.poll_interval = poll_interval
        self.initial_delay = max(0.0, initial_delay)
        self.history_size = history_size
        self.include = {name.lower() for name in include} if include else None
        self.exclude = {name.lower() for name in exclude} if exclude else set()
        self.event_handler = event_handler or self._default_event_handler
        self.exclude_self = exclude_self
        self._stop_event = threading.Event()
        self._supervisor_thread: Optional[threading.Thread] = None
        self._watchers: Dict[str, threading.Thread] = {}
        self._history: Dict[str, Deque[str]] = {}
        self._buffers: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()
        self._self_container_id = os.environ.get("HOSTNAME") if exclude_self else None
        self._initial_delay_consumed = False

    def start(self) -> None:
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            return
        self._stop_event.clear()
        self._supervisor_thread = threading.Thread(target=self._run_supervisor, name="docker-log-supervisor", daemon=True)
        self._supervisor_thread.start()

    def stop(self, timeout: Optional[float] = 10.0) -> None:
        self._stop_event.set()
        threads = []
        with self._lock:
            threads.extend(t for t in self._watchers.values())
        for thread in threads:
            thread.join(timeout=timeout)
        if self._supervisor_thread:
            self._supervisor_thread.join(timeout=timeout)

    def _run_supervisor(self) -> None:
        while not self._stop_event.is_set():
            if not self._initial_delay_consumed:
                self._initial_delay_consumed = True
                if self.initial_delay > 0:
                    self._stop_event.wait(self.initial_delay)
                    if self._stop_event.is_set():
                        break
            try:
                self._ensure_watchers()
            except Exception as exc:  # pragma: no cover - log but continue
                self.event_handler(
                    LogEvent(
                        container_id="monitor",
                        container_name="monitor",
                        timestamp=datetime.now(timezone.utc),
                        stream="stderr",
                        text=f"[monitor] Failed to refresh container list: {exc}",
                        is_error=True,
                    )
                )
            finally:
                self._stop_event.wait(self.poll_interval)

    def _ensure_watchers(self) -> None:
        active_ids = set()
        for container in self.client.containers.list(all=False):
            if not self._should_watch(container):
                continue
            active_ids.add(container.id)
            with self._lock:
                if container.id in self._watchers:
                    continue
                thread = threading.Thread(
                    target=self._watch_container,
                    args=(container.id,),
                    name=f"log-watch-{container.name}",
                    daemon=True,
                )
                self._watchers[container.id] = thread
                self._history[container.id] = deque(maxlen=self.history_size)
                self._buffers[container.id] = {"stdout": "", "stderr": ""}
                thread.start()
        with self._lock:
            # Clean up threads whose containers vanished.
            obsolete = [cid for cid in self._watchers if cid not in active_ids]
            for cid in obsolete:
                watcher = self._watchers.pop(cid)
                if watcher.is_alive():
                    continue
                self._history.pop(cid, None)
                self._buffers.pop(cid, None)

    def _watch_container(self, container_id: str) -> None:
        container = self.client.containers.get(container_id)
        metadata = {
            "image": container.image.tags[0] if container.image.tags else container.image.short_id,
            "name": container.name,
            **{f"label:{k}": v for k, v in (container.labels or {}).items()},
        }
        try:
            stream = container.attach(stream=True, logs=False, stdout=True, stderr=True, demux=True)
            for stdout_chunk, stderr_chunk in stream:
                if self._stop_event.is_set():
                    break
                if stdout_chunk:
                    self._handle_chunk(container, "stdout", stdout_chunk.decode("utf-8", errors="replace"), metadata)
                if stderr_chunk:
                    self._handle_chunk(container, "stderr", stderr_chunk.decode("utf-8", errors="replace"), metadata)
        except Exception as exc:
            event = LogEvent(
                container_id=container.id,
                container_name=container.name,
                timestamp=datetime.now(timezone.utc),
                stream="stderr",
                text=f"[monitor] Failed to tail container: {exc}",
                is_error=True,
                metadata=metadata,
            )
            self.event_handler(event)
        finally:
            with self._lock:
                self._watchers.pop(container_id, None)
                self._history.pop(container_id, None)
                self._buffers.pop(container_id, None)

    def _handle_chunk(self, container: Container, stream_name: str, text: str, metadata: Dict[str, str]) -> None:
        buffer = self._buffers[container.id][stream_name]
        combined = buffer + text
        lines = combined.splitlines()
        if combined and combined[-1] not in ("\n", "\r"):
            # Preserve incomplete line for next chunk.
            remainder = lines.pop() if lines else combined
        else:
            remainder = ""
        self._buffers[container.id][stream_name] = remainder
        for line in lines:
            cleaned = line.rstrip("\r")
            self._handle_line(container, stream_name, cleaned, metadata)

    def _handle_line(self, container: Container, stream_name: str, line: str, metadata: Dict[str, str]) -> None:
        timestamp = datetime.now(timezone.utc)
        history = self._history[container.id]
        history.append(line)
        is_error, match = self.classifier(line, metadata)
        stacktrace = None
        if is_error:
            stacktrace = self.stacktrace_extractor.extract(list(history))
        event = LogEvent(
            container_id=container.id,
            container_name=container.name,
            timestamp=timestamp,
            stream=stream_name,
            text=line,
            is_error=bool(is_error),
            rule_match=match,
            stacktrace=stacktrace,
            metadata=metadata,
        )
        self.event_handler(event)

    def _should_watch(self, container: Container) -> bool:
        if self.exclude_self and self._self_container_id and container.id.startswith(self._self_container_id):
            return False
        name = container.name.lower()
        if self.include is not None and name not in self.include:
            return False
        if self.exclude and name in self.exclude:
            return False
        if container.status != "running":
            return False
        return True

    @staticmethod
    def _default_event_handler(event: LogEvent) -> None:
        if not event.is_error:
            return
        prefix = "[ERROR]"
        stack = f"\n{event.stacktrace}" if event.stacktrace else ""
        rule = f" rule={event.rule_match.rule.name}" if event.rule_match else ""
        print(
            f"{prefix} {event.timestamp.isoformat()} {event.container_name}:{event.stream}{rule} {event.text}{stack}",
            flush=True,
        )
        try:
            agent_result = run_agents(event.text)
            if agent_result:
                print(f"[AGENTS] {agent_result}", flush=True)
        except Exception as exc:  # pragma: no cover - integration fallback
            print(f"[AGENTS] Failed to run agents for {event.container_name}: {exc}", flush=True)


def main() -> None:
    monitor = DockerLogMonitor()
    monitor.start()
    print("Docker log monitor started. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping monitor...")
        monitor.stop()


if __name__ == "__main__":
    main()
