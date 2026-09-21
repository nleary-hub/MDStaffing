"""Mutable schedule container with the indexes the solver and validator need."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .models import Assignment, Physician, ServiceLine, ShiftDef, sessions_overlap


class Schedule:
    def __init__(self, line: ServiceLine):
        self.line = line
        self.assignments: list[Assignment] = []
        self.by_day: dict[dt.date, list[Assignment]] = defaultdict(list)
        self.by_doc: dict[str, list[Assignment]] = defaultdict(list)
        self.by_doc_day: dict[tuple[str, dt.date], list[Assignment]] = defaultdict(list)
        self.by_slot: dict[tuple[dt.date, str, int], Assignment] = {}
        self.by_shift_day: dict[tuple[dt.date, str], list[Assignment]] = defaultdict(list)

    # -- mutation ---------------------------------------------------------
    def add(self, a: Assignment) -> None:
        self.assignments.append(a)
        self.by_day[a.date].append(a)
        self.by_doc[a.physician_id].append(a)
        self.by_doc_day[(a.physician_id, a.date)].append(a)
        self.by_slot[(a.date, a.shift_id, a.index)] = a
        self.by_shift_day[(a.date, a.shift_id)].append(a)

    def remove(self, a: Assignment) -> None:
        self.assignments.remove(a)
        self.by_day[a.date].remove(a)
        self.by_doc[a.physician_id].remove(a)
        self.by_doc_day[(a.physician_id, a.date)].remove(a)
        self.by_slot.pop((a.date, a.shift_id, a.index), None)
        self.by_shift_day[(a.date, a.shift_id)].remove(a)

    # -- queries ----------------------------------------------------------
    def effort_on(self, physician_id: str, day: dt.date) -> float:
        return sum(
            self.line.shift_by_id[a.shift_id].effort
            for a in self.by_doc_day[(physician_id, day)]
        )

    def session_conflict(self, physician_id: str, day: dt.date, shift: ShiftDef) -> bool:
        for a in self.by_doc_day[(physician_id, day)]:
            other = self.line.shift_by_id[a.shift_id]
            if sessions_overlap(other.session, shift.session):
                return True
        return False

    def works_on(self, physician_id: str, day: dt.date) -> bool:
        return bool(self.by_doc_day[(physician_id, day)])

    def clinical_days_in_week(self, physician_id: str, day: dt.date) -> int:
        monday = day - dt.timedelta(days=day.weekday())
        return sum(
            1
            for i in range(7)
            if self.effort_on(physician_id, monday + dt.timedelta(days=i)) > 0
        )

    def consecutive_run(self, physician_id: str, day: dt.date) -> int:
        """Length of the worked-day run that would include ``day``."""
        run = 1
        probe = day - dt.timedelta(days=1)
        while self.effort_on(physician_id, probe) > 0:
            run += 1
            probe -= dt.timedelta(days=1)
        probe = day + dt.timedelta(days=1)
        while self.effort_on(physician_id, probe) > 0:
            run += 1
            probe += dt.timedelta(days=1)
        return run

    def docs_on_day(self, day: dt.date) -> set[str]:
        return {a.physician_id for a in self.by_day[day]}

    def physician(self, pid: str) -> Physician:
        return self.line.physician_by_id[pid]

    def shift(self, sid: str) -> ShiftDef:
        return self.line.shift_by_id[sid]
