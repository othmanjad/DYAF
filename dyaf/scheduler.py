"""Rule scheduler — honors each rule's execution_frequency.

``run_pending(now)`` executes every enabled rule whose frequency interval
has elapsed since its last run. Designed to be driven by any external
ticker (cron, background thread, or the API's manual trigger).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .rules.engine import RuleEngine, RuleExecutionResult
from .rules.repository import RuleRepository


class RuleScheduler:
    def __init__(self, engine: RuleEngine, rules: RuleRepository):
        self.engine = engine
        self.rules = rules
        self._last_run: dict[str, datetime] = {}

    def due_rules(self, now: Optional[datetime] = None) -> list:
        now = now or datetime.now(timezone.utc)
        due = []
        for rule in self.rules.list():
            if not rule.enabled:
                continue
            last = self._last_run.get(rule.rule_id)
            if last is None or now - last >= rule.frequency_delta():
                due.append(rule)
        return due

    def run_pending(self, now: Optional[datetime] = None) -> list[RuleExecutionResult]:
        now = now or datetime.now(timezone.utc)
        results = []
        for rule in self.due_rules(now):
            results.append(self.engine.execute(rule, now=now))
            self._last_run[rule.rule_id] = now
        return results
