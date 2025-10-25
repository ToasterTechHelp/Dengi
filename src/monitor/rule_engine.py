"""
Simple rule-based classifier that flags log entries as errors.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Optional, Pattern, Sequence, Tuple


@dataclass(frozen=True)
class LogRule:
    """
    Describes a single pattern-based rule.
    """

    name: str
    pattern: Pattern[str]
    score: int = 1
    description: Optional[str] = None

    @staticmethod
    def from_keyword(keyword: str, *, case_sensitive: bool = False, score: int = 1, description: Optional[str] = None) -> "LogRule":
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(re.escape(keyword), flags)
        desc = description or f"Matches keyword {keyword!r}"
        return LogRule(name=f"keyword:{keyword}", pattern=pattern, score=score, description=desc)

    @staticmethod
    def from_regex(name: str, regex: str | Pattern[str], *, score: int = 1, description: Optional[str] = None, flags: int = re.IGNORECASE) -> "LogRule":
        pattern = re.compile(regex, flags) if isinstance(regex, str) else regex
        return LogRule(name=name, pattern=pattern, score=score, description=description)


@dataclass(frozen=True)
class RuleMatch:
    """
    Result of matching a rule.
    """

    rule: LogRule
    score: int


class RuleBasedClassifier:
    """
    Applies log rules to determine whether a log line should be treated as an error.
    """

    def __init__(self, rules: Optional[Sequence[LogRule]] = None):
        self._rules: Tuple[LogRule, ...] = tuple(rules) if rules else tuple(self._default_rules())

    @staticmethod
    def _default_rules() -> Iterable[LogRule]:
        keywords = [
            "error",
            "exception",
            "traceback",
            "panic",
            "fatal",
            "stack overflow",
            "segfault",
            "stacktrace",
        ]
        for kw in keywords:
            yield LogRule.from_keyword(kw)
        yield LogRule.from_regex(
            name="http-5xx",
            regex=r"HTTP/\d\.\d\"?\s5\d\d",
            description="HTTP server responded with a 5xx code",
        )
        yield LogRule.from_regex(
            name="uncaught-exception",
            regex=r"uncaught exception",
            description="Uncaught exception indicator",
        )

    def classify(self, text: str, metadata: Optional[dict] = None) -> Tuple[int, Optional[RuleMatch]]:
        """
        Returns (0 or 1, RuleMatch) depending on whether an error is detected.
        """
        if not text:
            return 0, None
        for rule in self._rules:
            if rule.pattern.search(text):
                return 1, RuleMatch(rule=rule, score=rule.score)
        return 0, None

    def __call__(self, text: str, metadata: Optional[dict] = None) -> Tuple[int, Optional[RuleMatch]]:
        return self.classify(text, metadata)
