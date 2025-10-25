"""
Heuristics for extracting stack traces from log buffers.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence


class StackTraceExtractor:
    """
    Attempts to recover a stack trace from a list of log lines.
    """

    PY_TRACEBACK_START = re.compile(r"Traceback \(most recent call last\):", re.IGNORECASE)
    STACK_FRAME = re.compile(r"^\s*(at\s+[\w.$<>:/-]+\(.+\)|File\s+\".+\", line \d+|Caused by: .+)$", re.IGNORECASE)
    ERROR_HEADER = re.compile(r"(Exception|Error|panic|TRACEBACK)", re.IGNORECASE)

    def __init__(self, min_frame_lines: int = 2):
        self.min_frame_lines = min_frame_lines

    def extract(self, lines: Sequence[str]) -> Optional[str]:
        """
        Returns the most recent stack trace-like block found in `lines`.
        """
        normalized = [line.rstrip("\n") for line in lines if line]
        if not normalized:
            return None
        python_trace = self._extract_python_trace(normalized)
        if python_trace:
            return python_trace
        generic_trace = self._extract_generic_trace(normalized)
        if generic_trace:
            return generic_trace
        return None

    def _extract_python_trace(self, lines: Sequence[str]) -> Optional[str]:
        for idx, line in enumerate(lines):
            if self.PY_TRACEBACK_START.search(line):
                block = [line]
                for follow in lines[idx + 1 :]:
                    if not follow.strip():
                        break
                    block.append(follow)
                if len(block) >= self.min_frame_lines:
                    return "\n".join(block)
        return None

    def _extract_generic_trace(self, lines: Sequence[str]) -> Optional[str]:
        block: List[str] = []
        capturing = False
        for line in reversed(lines):
            if capturing:
                if self._looks_like_stack_frame(line) or not block:
                    block.insert(0, line)
                else:
                    break
            else:
                if self._looks_like_stack_frame(line) or self.ERROR_HEADER.search(line):
                    capturing = True
                    block.insert(0, line)
        if len(block) >= self.min_frame_lines:
            return "\n".join(block)
        return None

    def _looks_like_stack_frame(self, line: str) -> bool:
        return bool(self.STACK_FRAME.search(line))


def extract_stack_trace(lines: Iterable[str]) -> Optional[str]:
    """
    Convenience function to grab a stack trace without instantiating the class.
    """
    return StackTraceExtractor().extract(list(lines))
