"""Entry point that wires the monitor, LLM orchestrator, and GitHub hotfix workflow."""
from __future__ import annotations

import logging
import signal
import sys
import time

from dotenv import load_dotenv

from src.monitor.service import DockerLogMonitor
from src.workflows import ContinuousHotfixPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dengi.main")


def main() -> int:
    load_dotenv()
    pipeline = ContinuousHotfixPipeline()
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
            time.sleep(1.0)
    finally:
        monitor.stop()
        logger.info("Monitor stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
