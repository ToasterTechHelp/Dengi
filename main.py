"""Entry point that wires the monitor, LLM orchestrator, and GitHub hotfix workflow."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from dotenv import load_dotenv

from src.monitor.service import DockerLogMonitor
from src.workflows import ContinuousHotfixPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dengi.main")


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    load_dotenv()
    fail_fast = _truthy(os.getenv("DENGI_FAIL_FAST"), default=True)
    fatal_event = threading.Event()
    fatal_errors: list[BaseException] = []

    def _record_fatal(exc: BaseException) -> None:
        fatal_errors.append(exc)
        fatal_event.set()

    pipeline = ContinuousHotfixPipeline(fail_fast=fail_fast, on_fatal=_record_fatal)
    monitor = DockerLogMonitor(event_handler=pipeline.handle_event)
    monitor.start()
    logger.info("Continuous hotfix pipeline is running. Press Ctrl+C to stop.")

    stop = False

    def _handle_sigint(signum, frame):  # pragma: no cover - signal wiring
        nonlocal stop
        stop = True
        logger.info("Shutting down monitor...")

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    try:
        while not stop:
            if fatal_event.wait(timeout=1.0):
                stop = True
    finally:
        monitor.stop()
        logger.info("Monitor stopped.")
    if fatal_errors:
        logger.error("Fatal pipeline error encountered: %s", fatal_errors[-1])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
