"""The scheduling engine.

The pipeline mirrors how a service-line scheduler actually works by hand:

1. **Block out who cannot work** — vacation, approved time off, protected
   admin/medical-director time.
2. **Lock in what is already decided** — pre-committed assignments and approved
   "must work this day" requests.
3. **Fill the scarce, rule-bound work first** — procedural and diagnostic duties
   that only a handful of physicians are credentialed for, in tier order and
   scarcest-first, picking from an equity rotation.
4. **Fill core coverage** — hospital rounding, consults, clinic blocks.
5. **Back-fill** — satisfy minimum-census rules, honour scheduling requests, and
   place whoever is left into the default clinic pool.
6. **Repair** — targeted swaps to fill anything that is still short.

Every stage honours hard constraints; the choice *within* the feasible set is
made by the fairness ledger plus request preferences.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

from .fairness import Ledger, debt, target_shares
from .models import (
    Assignment,
    Physician,
    ServiceLine,
    ShiftDef,
    Slot,
    Violation,
    sessions_overlap,
)
from .schedule import Schedule
from .validate import soft_rule_pressure, validate_schedule, would_break_hard_max

DEFAULT_WEIGHTS = {
    "fairness": 10.0,        # share-of-duty equity
    "weekend": 6.0,          # weekend/holiday burden equity
    "load": 3.0,             # overall clinical load vs FTE
    "soft_rule": 8.0,        # strain on soft census rules
    "request": 12.0,         # honouring physician requests
    "soft_admin": 15.0,      # breaking a soft admin block
    "recency": 2.0,          # spacing repeats of the same duty
    "continuity": 1000.0,    # keeping multi-day blocks intact
    "weekly_cap": 25.0,      # approaching a physician's weekly day cap
    "consecutive": 20.0,     # approaching the consecutive-day cap
}


@dataclass
class SolveResult:
    schedule: Schedule
    slots: list[Slot]
    violations: list[Violation]
    ledger: Ledger
    unfilled: list[Slot] = field(default_factory=list)
    #: shares[fairness_group][physician_id] -> expected fraction of that duty,
    #: derived from FTE, credentialing and availability during the period.
    shares: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def hard_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "hard"]


# ---------------------------------------------------------------------------


class Scheduler:
    def __init__(self, line: ServiceLine, ledger: Ledger | None = None, seed: int = 0):
        self.line = line
        self.ledger = (ledger or Ledger()).copy()
        self.seed = seed
        self.weights = dict(DEFAULT_WEIGHTS)
        self.weights.update(line.settings.get("weights") or {})
        self.max_background = int(line.settings.get("max_background_per_day", 2))
        self.daily_capacity = float(line.settings.get("daily_capacity", 1.0))
        self.default_fill = line.settings.get("default_fill_shift")
        self.repair_passes = int(line.settings.get("repair_passes", 2))
        self.at_risk_slack = int(line.settings.get("at_risk_slack", 1))
        self._occurrence_cache: dict[str, list[dt.date]] = {}
        self._shares: dict[str, dict[str, float]] = {}
        self._load_target: dict[str, float] = {}

    # -- calendar ---------------------------------------------------------
    def occurrences(self, shift: ShiftDef) -> list[dt.date]:
        if shift.id not in self._occurrence_cache:
            self._occurrence_cache[shift.id] = [
                d for d in self.line.days() if shift.recurrence.matches(d, self.line.holidays)
            ]
        return self._occurrence_cache[shift.id]

    def build_slots(self) -> list[Slot]:
        slots: list[Slot] = []
        for shift in self.line.shifts:
            for day in self.occurrences(shift):
                for i in range(shift.min_count):
                    slots.append(Slot(day, shift.id, i, required=True))
                extra = (shift.max_count or shift.min_count) - shift.min_count
                for i in range(extra):
                    slots.append(Slot(day, shift.id, shift.min_count + i, required=False))
        return slots

    # -- eligibility ------------------------------------------------------
    def is_eligible(self, doc: Physician, shift: ShiftDef) -> bool:
        if shift.id in doc.excluded_shifts:
            return False
        if doc.shift_weights.get(shift.id, 1.0) <= 0:
            return False
        if shift.eligible and doc.id not in shift.eligible:
            return False
        if shift.specialties and doc.specialty not in shift.specialties:
            return False
        if shift.required_tags and not set(shift.required_tags).issubset(doc.tags):
            return False
        if shift.any_tags and not (set(shift.any_tags) & doc.tags):
            return False
        if shift.required_skills and not doc.has_skills(shift.required_skills):
            return False
        for group in shift.any_skills:
            if not (set(group) & doc.skills):
                return False
        return True

    def eligible_pool(self, shift: ShiftDef) -> list[Physician]:
        return [d for d in self.line.physicians if self.is_eligible(d, shift)]

    def _availability_fraction(self, doc: Physician, days: list[dt.date]) -> float:
        if not days:
            return 0.0
        free = sum(1 for d in days if not doc.is_unavailable(d))
        return free / len(days)

    def prepare_shares(self) -> None:
        """Per fairness group: each physician's expected share of the duty."""
        groups: dict[str, list[ShiftDef]] = {}
        for shift in self.line.shifts:
            groups.setdefault(shift.fairness_key(), []).append(shift)
        for group, shifts in groups.items():
            days = sorted({d for s in shifts for d in self.occurrences(s)})
            weights: dict[str, float] = {}
            for doc in self.line.physicians:
                if not any(self.is_eligible(doc, s) for s in shifts):
                    continue
                per_shift = max(doc.shift_weights.get(s.id, 1.0) for s in shifts)
                weights[doc.id] = doc.fte * per_shift * self._availability_fraction(doc, days)
            self._shares[group] = target_shares(weights)

        # weekend / holiday burden, pooled across every duty
        for special in ("_weekend", "_holiday"):
            if special == "_weekend":
                days = [d for d in self.line.days() if d.weekday() >= 5]
            else:
                days = [d for d in self.line.days() if d in self.line.holidays]
            weights = {
                doc.id: doc.fte * self._availability_fraction(doc, days or self.line.days())
                for doc in self.line.physicians
            }
            self._shares[special] = target_shares(weights)

        # overall clinical load target (used to keep totals sane)
        all_days = self.line.days()
        weights = {
            d.id: d.fte * self._availability_fraction(d, all_days) for d in self.line.physicians
        }
        self._load_target = target_shares(weights)

    # -- feasibility ------------------------------------------------------
    def block_reasons(self, sched: Schedule, doc: Physician, shift: ShiftDef, day: dt.date,
                      collect: bool = False) -> list[str]:
        """Why this physician cannot take this duty today.

        With ``collect=False`` it stops at the first blocker (the hot path used
        by the solver); with ``collect=True`` it reports all of them, which is
        what ``mdstaffing explain`` prints.
        """
        out: list[str] = []

        def fail(reason: str) -> bool:
            out.append(reason)
            return not collect  # stop early unless we are diagnosing

        if not self.is_eligible(doc, shift):
            if fail("not credentialed for this duty"):
                return out
        reason = doc.is_unavailable(day)
        if reason and fail(reason):
            return out
        if any(a.physician_id == doc.id for a in sched.by_shift_day[(day, shift.id)]):
            if fail("already covering this duty today"):
                return out
        if sched.session_conflict(doc.id, day, shift):
            clash = ", ".join(
                sched.shift(a.shift_id).label
                for a in sched.by_doc_day[(doc.id, day)]
                if sessions_overlap(sched.shift(a.shift_id).session, shift.session)
            )
            if fail(f"already booked: {clash}"):
                return out
        if sched.effort_on(doc.id, day) + shift.effort > self.daily_capacity + 1e-9:
            # A session clash already explains this; don't say it twice.
            if not any(r.startswith("already booked") for r in out):
                if fail("no capacity left in the day"):
                    return out
        if shift.session == "background":
            bg = sum(
                1 for a in sched.by_doc_day[(doc.id, day)]
                if sched.shift(a.shift_id).session == "background"
            )
            if bg >= self.max_background and fail(
                f"already carrying {bg} background duties"
            ):
                return out
        for block in doc.admin_sessions(day, self.line.holidays):
            if block.hard and sessions_overlap(block.session, shift.session):
                if fail(f"protected time: {block.label}"):
                    return out
        for req in self.line.requests:
            if req.physician_id != doc.id or not req.covers(day):
                continue
            if req.kind == "off_hard":
                if fail(f"approved time off{': ' + req.note if req.note else ''}"):
                    return out
            if req.kind == "avoid_shift" and req.shift_id == shift.id and req.weight >= 10:
                if fail("declared unavailable for this duty"):
                    return out

        # Caps count *clinical* days; a background reading list alone does not
        # make a day count, so test against effort already committed.
        new_clinical_day = shift.effort > 0 and sched.effort_on(doc.id, day) == 0
        if doc.max_clinical_days_per_week is not None and new_clinical_day:
            used = sched.clinical_days_in_week(doc.id, day)
            if used + 1 > doc.max_clinical_days_per_week and fail(
                f"at weekly cap ({used}/{doc.max_clinical_days_per_week:g} days)"
            ):
                return out
        if new_clinical_day:
            run = sched.consecutive_run(doc.id, day)
            if run > doc.max_consecutive_clinical_days and fail(
                f"would make {run} consecutive clinical days"
                f" (cap {doc.max_consecutive_clinical_days})"
            ):
                return out
        if would_break_hard_max(sched, day, shift.id, doc.id):
            names = [
                r.id for r in self.line.rules
                if r.max_count is not None and r.severity == "hard"
                and r.matches_assignment(shift, doc)
            ]
            if fail("would break census rule " + ", ".join(names) if names
                    else "would break a census rule"):
                return out
        return out

    def feasible(self, sched: Schedule, doc: Physician, shift: ShiftDef,
                 day: dt.date) -> bool:
        return not self.block_reasons(sched, doc, shift, day)

    # -- scoring ----------------------------------------------------------
    def _request_score(self, doc: Physician, shift: ShiftDef, day: dt.date) -> float:
        score = 0.0
        for req in self.line.requests:
            if req.physician_id != doc.id or not req.covers(day):
                continue
            if req.kind == "off_soft":
                score += req.weight
            elif req.kind == "on_soft" and (req.shift_id in (None, shift.id)):
                score -= req.weight
            elif req.kind == "prefer_shift" and req.shift_id == shift.id:
                score -= req.weight
            elif req.kind == "avoid_shift" and req.shift_id == shift.id:
                score += req.weight
        return score

    def _recency(self, sched: Schedule, doc: Physician, shift: ShiftDef, day: dt.date) -> float:
        group = shift.fairness_key()
        last = self.ledger.last_worked.get(group, {}).get(doc.id)
        if not last:
            return 0.0
        gap = (day - dt.date.fromisoformat(last)).days
        if gap <= 0:
            return 1.0
        return max(0.0, 1.0 - gap / 14.0)

    def _load_pressure(self, sched: Schedule, doc: Physician) -> float:
        total = sum(sched.shift(a.shift_id).effort for a in sched.by_doc[doc.id])
        grand = sum(sched.shift(a.shift_id).effort for a in sched.assignments)
        expected = self._load_target.get(doc.id, 0.0) * grand
        return total - expected

    def score(self, sched: Schedule, doc: Physician, shift: ShiftDef, day: dt.date) -> float:
        w = self.weights
        group = shift.fairness_key()
        shares = self._shares.get(group, {})
        pool_total = self.ledger.total(group)
        s = w["fairness"] * debt(self.ledger, group, doc.id, shares, pool_total)

        if day.weekday() >= 5 or day in self.line.holidays:
            special = "_holiday" if day in self.line.holidays else "_weekend"
            s += w["weekend"] * debt(self.ledger, special, doc.id,
                                     self._shares.get(special, {}), self.ledger.total(special))

        s += w["load"] * self._load_pressure(sched, doc)
        s += w["soft_rule"] * soft_rule_pressure(sched, day, shift.id, doc.id)
        s += w["request"] * self._request_score(doc, shift, day)
        s += w["recency"] * self._recency(sched, doc, shift, day)

        for block in doc.admin_sessions(day, self.line.holidays):
            if not block.hard and sessions_overlap(block.session, shift.session):
                s += w["soft_admin"]

        if doc.max_clinical_days_per_week is not None and shift.effort > 0:
            used = sched.clinical_days_in_week(doc.id, day)
            headroom = doc.max_clinical_days_per_week - used
            if headroom <= 1:
                s += w["weekly_cap"] * (2 - max(headroom, 0))
        if shift.effort > 0 and sched.effort_on(doc.id, day) == 0:
            run = sched.consecutive_run(doc.id, day)
            if run >= max(doc.max_consecutive_clinical_days - 1, 1):
                s += w["consecutive"]

        # stable, config-independent tie-break
        h = hashlib.md5(f"{self.seed}:{doc.id}:{shift.id}:{day}".encode()).hexdigest()
        s += int(h[:6], 16) / 0xFFFFFF * 0.01
        return s

    # -- block continuity -------------------------------------------------
    def _block_incumbent(self, sched: Schedule, shift: ShiftDef, day: dt.date,
                         index: int) -> str | None:
        """Physician who should stay on this duty because a block is running."""
        if shift.block_days <= 1:
            return None
        occ = self.occurrences(shift)
        if day not in occ:
            return None
        pos = occ.index(day)
        if pos == 0:
            return None
        prev = sched.by_slot.get((occ[pos - 1], shift.id, index))
        if not prev:
            return None
        run, k = 1, pos - 1
        while k > 0:
            earlier = sched.by_slot.get((occ[k - 1], shift.id, index))
            if not earlier or earlier.physician_id != prev.physician_id:
                break
            run += 1
            k -= 1
        return prev.physician_id if run < shift.block_days else None

    # -- assignment -------------------------------------------------------
    def assign(self, sched: Schedule, slot: Slot, reason: str) -> Assignment | None:
        shift = self.line.shift_by_id[slot.shift_id]
        candidates = [
            d for d in self.line.physicians if self.feasible(sched, d, shift, slot.date)
        ]
        if not candidates:
            return None
        incumbent = self._block_incumbent(sched, shift, slot.date, slot.index)
        best = min(
            candidates,
            key=lambda d: (
                self.score(sched, d, shift, slot.date)
                - (self.weights["continuity"] if d.id == incumbent else 0.0)
            ),
        )
        a = Assignment(slot.date, shift.id, best.id, slot.index,
                       reason="block" if best.id == incumbent else reason)
        self._commit(sched, a)
        return a

    def _commit(self, sched: Schedule, a: Assignment) -> None:
        sched.add(a)
        shift = self.line.shift_by_id[a.shift_id]
        weight = self.line.physician_by_id[a.physician_id].shift_weights.get(a.shift_id, 1.0)
        credit = shift.credit / weight if weight else shift.credit
        self.ledger.add(shift.fairness_key(), a.physician_id, credit, a.date.isoformat())
        if a.date.weekday() >= 5:
            self.ledger.add("_weekend", a.physician_id, shift.credit, a.date.isoformat())
        if a.date in self.line.holidays:
            self.ledger.add("_holiday", a.physician_id, shift.credit, a.date.isoformat())

    def _uncommit(self, sched: Schedule, a: Assignment) -> None:
        sched.remove(a)
        shift = self.line.shift_by_id[a.shift_id]
        weight = self.line.physician_by_id[a.physician_id].shift_weights.get(a.shift_id, 1.0)
        credit = shift.credit / weight if weight else shift.credit
        self.ledger.add(shift.fairness_key(), a.physician_id, -credit)
        if a.date.weekday() >= 5:
            self.ledger.add("_weekend", a.physician_id, -shift.credit)
        if a.date in self.line.holidays:
            self.ledger.add("_holiday", a.physician_id, -shift.credit)

    # -- stages -----------------------------------------------------------
    def _place_locked(self, sched: Schedule) -> list[Violation]:
        out: list[Violation] = []
        pinned: list[Assignment] = list(self.line.locked)
        for req in self.line.requests:
            if req.kind != "on_hard" or not req.shift_id:
                continue
            shift = self.line.shift_by_id[req.shift_id]
            day = req.start
            while day <= req.end:
                if shift.recurrence.matches(day, self.line.holidays):
                    pinned.append(Assignment(day, shift.id, req.physician_id, reason="request"))
                day += dt.timedelta(days=1)
        for a in pinned:
            shift = self.line.shift_by_id[a.shift_id]
            if not shift.recurrence.matches(a.date, self.line.holidays):
                out.append(Violation(a.date, "locked_invalid", "hard",
                                     f"{shift.label} does not occur on this date", shift_id=shift.id))
                continue
            index = a.index
            cap = shift.max_count or shift.min_count
            while (a.date, shift.id, index) in sched.by_slot and index < cap:
                index += 1
            if (a.date, shift.id, index) in sched.by_slot:
                out.append(Violation(a.date, "locked_overflow", "hard",
                                     f"{shift.label} has no free position for a pinned assignment",
                                     shift_id=shift.id, physician_id=a.physician_id))
                continue
            self._commit(sched, Assignment(a.date, shift.id, a.physician_id, index, a.reason))
        return out

    def _scarcity(self, sched: Schedule, shift: ShiftDef, day: dt.date) -> int:
        return sum(1 for d in self.line.physicians if self.feasible(sched, d, shift, day))

    def _fill_all(self, sched: Schedule, slots: list[Slot]) -> list[Slot]:
        """Fill every required slot, most-constrained duty first.

        Tiers express intent ("procedural work is scheduled before clinic"), but
        intent alone is not enough: a tier-1 duty with six credentialed
        physicians must not consume the only two people who can run the CCU. So
        the engine walks day by day and always places the duty with the least
        slack (eligible physicians minus positions still to fill), using tier as
        the tie-break. That naturally schedules the scarce procedural and
        diagnostic work first while protecting thin core services.
        """
        unfilled: list[Slot] = []
        by_day: dict[dt.date, list[Slot]] = {}
        for slot in slots:
            if not slot.required or slot.key in sched.by_slot:
                continue
            by_day.setdefault(slot.date, []).append(slot)

        for day in sorted(by_day):
            todo = [s for s in by_day[day] if s.key not in sched.by_slot]
            while todo:
                need: dict[str, int] = {}
                for s in todo:
                    need[s.shift_id] = need.get(s.shift_id, 0) + 1
                slack: dict[str, tuple] = {}
                for sid, count in need.items():
                    shift = self.line.shift_by_id[sid]
                    room = self._scarcity(sched, shift, day) - count
                    # At-risk duties (no spare candidates) jump the queue whatever
                    # their tier; everything else is scheduled in tier order so
                    # procedural work still precedes residual clinic fill.
                    slack[sid] = (
                        (0, room, 0 if shift.hard else 1, shift.tier, sid)
                        if room <= self.at_risk_slack
                        else (1, shift.tier, room, sid)
                    )
                todo.sort(key=lambda s: (slack[s.shift_id], s.index))
                slot = todo.pop(0)
                if slot.key in sched.by_slot:
                    continue
                if self.assign(sched, slot, "rotation") is None:
                    unfilled.append(slot)
        return unfilled

    def _satisfy_min_rules(self, sched: Schedule, slots: list[Slot]) -> None:
        """Add optional-slot assignments where a minimum-census rule is short."""
        optional_by_day: dict[dt.date, list[Slot]] = {}
        for slot in slots:
            if not slot.required and slot.key not in sched.by_slot:
                optional_by_day.setdefault(slot.date, []).append(slot)
        for day in self.line.days():
            for rule in self.line.rules:
                if rule.min_count is None or not rule.recurrence.matches(day, self.line.holidays):
                    continue
                for _ in range(rule.min_count * 2):  # bounded
                    from .validate import rule_count
                    if rule_count(rule, day, sched) >= rule.min_count:
                        break
                    options: list[tuple[float, Slot, Physician]] = []
                    for slot in optional_by_day.get(day, []):
                        if slot.key in sched.by_slot:
                            continue
                        shift = self.line.shift_by_id[slot.shift_id]
                        for doc in self.line.physicians:
                            if not rule.matches_assignment(shift, doc):
                                continue
                            if not self.feasible(sched, doc, shift, day):
                                continue
                            options.append((self.score(sched, doc, shift, day), slot, doc))
                    if not options:
                        break
                    options.sort(key=lambda t: t[0])
                    _, slot, doc = options[0]
                    self._commit(sched, Assignment(day, slot.shift_id, doc.id, slot.index,
                                                   "rule_fill"))

    def _default_fill(self, sched: Schedule, slots: list[Slot]) -> None:
        """Place still-free physicians into the default clinic/flex pool."""
        if not self.default_fill:
            return
        shift = self.line.shift_by_id.get(self.default_fill)
        if shift is None:
            return
        free_slots: dict[dt.date, list[Slot]] = {}
        for slot in slots:
            if slot.shift_id == shift.id and slot.key not in sched.by_slot:
                free_slots.setdefault(slot.date, []).append(slot)
        for day in self.line.days():
            open_slots = sorted(free_slots.get(day, []), key=lambda s: s.index)
            while open_slots:
                slot = open_slots.pop(0)
                if slot.key in sched.by_slot:
                    continue
                pool = [
                    d for d in self.line.physicians
                    if self.feasible(sched, d, shift, day) and sched.effort_on(d.id, day) == 0
                ]
                if not pool:
                    break
                best = min(pool, key=lambda d: self.score(sched, d, shift, day))
                self._commit(sched, Assignment(day, shift.id, best.id, slot.index, "backfill"))

    def _repair(self, sched: Schedule, unfilled: list[Slot]) -> list[Slot]:
        """One-level swaps: free a qualified physician by moving their other duty."""
        still: list[Slot] = []
        for slot in unfilled:
            if slot.key in sched.by_slot:
                continue
            shift = self.line.shift_by_id[slot.shift_id]
            fixed = False
            for doc in self.eligible_pool(shift):
                if doc.is_unavailable(slot.date) or fixed:
                    continue
                blockers = [
                    a for a in list(sched.by_doc_day[(doc.id, slot.date)])
                    if sessions_overlap(sched.shift(a.shift_id).session, shift.session)
                    or sched.effort_on(doc.id, slot.date) + shift.effort > self.daily_capacity
                ]
                if len(blockers) != 1:
                    continue
                blocker = blockers[0]
                blocker_shift = self.line.shift_by_id[blocker.shift_id]
                if blocker_shift.tier < shift.tier:
                    continue  # never rob a more specialised duty
                self._uncommit(sched, blocker)
                if not self.feasible(sched, doc, shift, slot.date):
                    self._commit(sched, blocker)
                    continue
                replacement = [
                    d for d in self.line.physicians
                    if d.id != doc.id and self.feasible(sched, d, blocker_shift, blocker.date)
                ]
                if not replacement:
                    self._commit(sched, blocker)
                    continue
                stand_in = min(
                    replacement, key=lambda d: self.score(sched, d, blocker_shift, blocker.date)
                )
                self._commit(sched, Assignment(slot.date, shift.id, doc.id, slot.index, "repair"))
                if not self.feasible(sched, stand_in, blocker_shift, blocker.date):
                    still_ok = [
                        d for d in replacement
                        if self.feasible(sched, d, blocker_shift, blocker.date)
                    ]
                    if not still_ok:
                        # the move would leave the blocker's duty short: undo it
                        self._uncommit(sched, sched.by_slot[(slot.date, shift.id, slot.index)])
                        self._commit(sched, blocker)
                        continue
                    stand_in = min(
                        still_ok,
                        key=lambda d: self.score(sched, d, blocker_shift, blocker.date),
                    )
                self._commit(sched, Assignment(blocker.date, blocker_shift.id, stand_in.id,
                                               blocker.index, "repair"))
                fixed = True
            if not fixed:
                still.append(slot)
        return still

    # -- entry point ------------------------------------------------------
    def solve(self) -> SolveResult:
        self.prepare_shares()
        sched = Schedule(self.line)
        slots = self.build_slots()
        problems = self._place_locked(sched)

        unfilled = self._fill_all(sched, slots)

        for _ in range(self.repair_passes):
            if not unfilled:
                break
            before = len(unfilled)
            unfilled = self._repair(sched, unfilled)
            if len(unfilled) == before:
                break

        self._satisfy_min_rules(sched, slots)
        self._default_fill(sched, slots)

        violations = problems + validate_schedule(sched, slots)
        return SolveResult(sched, slots, violations, self.ledger, unfilled,
                           shares={k: dict(v) for k, v in self._shares.items()})


def solve(line: ServiceLine, ledger: Ledger | None = None, seed: int = 0) -> SolveResult:
    return Scheduler(line, ledger, seed).solve()
