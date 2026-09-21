"""Equity ledger.

Fairness here means "share of a duty proportional to how much of that duty you
are expected to carry", not "everybody does the same number". A 0.6 FTE
physician who reads nuclear should get ~60% of a full-timer's nuclear days, and
someone who is not credentialed for the cath lab should not dilute the
interventionalists' target shares.

The ledger is persisted between publishing periods so rotations stay fair over
a year, not just inside one month.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Ledger:
    #: credits[group][physician_id] -> accumulated credit
    credits: dict[str, dict[str, float]] = field(default_factory=lambda: defaultdict(dict))
    #: last date (ISO) a physician worked a given group, for tie-breaking
    last_worked: dict[str, dict[str, str]] = field(default_factory=lambda: defaultdict(dict))

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None) -> "Ledger":
        led = cls()
        if not path:
            return led
        p = Path(path)
        if not p.exists():
            return led
        raw = json.loads(p.read_text())
        for group, docs in (raw.get("credits") or {}).items():
            led.credits[group] = {k: float(v) for k, v in docs.items()}
        for group, docs in (raw.get("last_worked") or {}).items():
            led.last_worked[group] = dict(docs)
        return led

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {"credits": {k: v for k, v in self.credits.items()},
                 "last_worked": {k: v for k, v in self.last_worked.items()}},
                indent=2,
                sort_keys=True,
            )
        )

    # -- accounting -------------------------------------------------------
    def get(self, group: str, physician_id: str) -> float:
        return self.credits.get(group, {}).get(physician_id, 0.0)

    def add(self, group: str, physician_id: str, amount: float, day: str | None = None) -> None:
        self.credits.setdefault(group, {})
        self.credits[group][physician_id] = self.get(group, physician_id) + amount
        if day:
            prior = self.last_worked.setdefault(group, {}).get(physician_id)
            if prior is None or day > prior:
                self.last_worked[group][physician_id] = day

    def copy(self) -> "Ledger":
        led = Ledger()
        led.credits = {g: dict(d) for g, d in self.credits.items()}
        led.last_worked = {g: dict(d) for g, d in self.last_worked.items()}
        return led

    def total(self, group: str) -> float:
        return sum(self.credits.get(group, {}).values())


def target_shares(weights: dict[str, float]) -> dict[str, float]:
    """Normalise eligibility weights into fractional targets that sum to 1."""
    total = sum(w for w in weights.values() if w > 0)
    if total <= 0:
        return {pid: 0.0 for pid in weights}
    return {pid: (w / total if w > 0 else 0.0) for pid, w in weights.items()}


def debt(ledger: Ledger, group: str, physician_id: str, shares: dict[str, float],
         pool_total: float) -> float:
    """How far ahead (positive) or behind (negative) a physician is on a duty.

    A physician with a large negative debt is "owed" the next one.
    """
    expected = shares.get(physician_id, 0.0) * pool_total
    return ledger.get(group, physician_id) - expected
