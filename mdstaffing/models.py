"""Domain model for the service-line physician scheduler.

Everything the solver needs is expressed as plain dataclasses built from a YAML
config, so the scheduling rules live in data (editable by a scheduler/admin)
rather than in code.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_INDEX = {name: i for i, name in enumerate(WEEKDAYS)}

#: A session is the slice of the day a duty occupies. ``full`` blocks the whole
#: day; ``am``/``pm`` are half days; ``background`` duties (e.g. reading a study
#: list) can stack on top of clinical work.
SESSIONS = ("full", "am", "pm", "background")


class ConfigError(ValueError):
    """Raised when the YAML configuration is malformed or self-inconsistent."""


def parse_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        return dt.date.fromisoformat(value.strip())
    raise ConfigError(f"cannot parse date from {value!r}")


def parse_weekday(value: Any) -> int:
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ConfigError(f"weekday index out of range: {value}")
    key = str(value).strip().lower()[:3]
    if key not in WEEKDAY_INDEX:
        raise ConfigError(f"unknown weekday {value!r}")
    return WEEKDAY_INDEX[key]


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def sessions_overlap(a: str, b: str) -> bool:
    """True when two duties in the same day cannot be held by one physician."""
    if a == "background" or b == "background":
        return False
    if a == "full" or b == "full":
        return True
    return a == b


# ---------------------------------------------------------------------------
# recurrence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NthWeekday:
    week: int  # 1..5, or -1 for "last"
    weekday: int

    def matches(self, day: dt.date) -> bool:
        if day.weekday() != self.weekday:
            return False
        if self.week == -1:
            return (day + dt.timedelta(days=7)).month != day.month
        return (day.day - 1) // 7 + 1 == self.week


@dataclass
class Recurrence:
    """Which calendar days a demand (or rule) applies to.

    All specified filters are ANDed except ``dates``, which is a union: an
    explicitly listed date always matches (unless excluded).
    """

    weekdays: list[int] = field(default_factory=list)
    nth_weekdays: list[NthWeekday] = field(default_factory=list)
    months: list[int] = field(default_factory=list)
    dates: list[dt.date] = field(default_factory=list)
    exclude_dates: list[dt.date] = field(default_factory=list)
    interval_weeks: int = 1
    anchor: dt.date | None = None
    include_holidays: bool = False

    @classmethod
    def from_config(cls, cfg: Any) -> "Recurrence":
        if cfg is None:
            return cls(weekdays=[0, 1, 2, 3, 4])
        if isinstance(cfg, str) and cfg.lower() in ("daily", "everyday"):
            return cls(weekdays=list(range(7)))
        if not isinstance(cfg, dict):
            raise ConfigError(f"recurrence must be a mapping, got {cfg!r}")
        nth = []
        for item in as_list(cfg.get("nth_weekday_of_month")):
            if not isinstance(item, dict):
                raise ConfigError(f"nth_weekday_of_month entry must be a mapping: {item!r}")
            nth.append(NthWeekday(int(item["week"]), parse_weekday(item["weekday"])))
        return cls(
            weekdays=[parse_weekday(d) for d in as_list(cfg.get("weekdays"))],
            nth_weekdays=nth,
            months=[int(m) for m in as_list(cfg.get("months"))],
            dates=[parse_date(d) for d in as_list(cfg.get("dates"))],
            exclude_dates=[parse_date(d) for d in as_list(cfg.get("exclude_dates"))],
            interval_weeks=int(cfg.get("interval_weeks", 1) or 1),
            anchor=parse_date(cfg["anchor"]) if cfg.get("anchor") else None,
            include_holidays=bool(cfg.get("include_holidays", False)),
        )

    def matches(self, day: dt.date, holidays: set[dt.date] | None = None) -> bool:
        holidays = holidays or set()
        if day in self.exclude_dates:
            return False
        if day in self.dates:
            return True
        if day in holidays and not self.include_holidays:
            return False
        if self.months and day.month not in self.months:
            return False
        if self.weekdays and day.weekday() not in self.weekdays:
            return False
        if self.nth_weekdays and not any(n.matches(day) for n in self.nth_weekdays):
            return False
        if not self.weekdays and not self.nth_weekdays:
            # No weekday filter and no explicit dates -> nothing recurs.
            return False
        if self.interval_weeks > 1:
            anchor = self.anchor or dt.date(2020, 1, 6)  # a Monday
            anchor_monday = anchor - dt.timedelta(days=anchor.weekday())
            day_monday = day - dt.timedelta(days=day.weekday())
            weeks = (day_monday - anchor_monday).days // 7
            if weeks % self.interval_weeks != 0:
                return False
        return True


# ---------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------


@dataclass
class Unavailability:
    start: dt.date
    end: dt.date
    reason: str = "vacation"

    def covers(self, day: dt.date) -> bool:
        return self.start <= day <= self.end


@dataclass
class Physician:
    id: str
    name: str
    specialty: str = "cardiology"
    skills: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    fte: float = 1.0
    #: Clinical sessions available per week after admin/protected time.
    max_clinical_days_per_week: float | None = None
    max_consecutive_clinical_days: int = 7
    #: Recurring protected time, e.g. a medical director's admin day.
    admin: list["AdminBlock"] = field(default_factory=list)
    unavailable: list[Unavailability] = field(default_factory=list)
    #: Duties this physician must never be given, even if skill-eligible.
    excluded_shifts: set[str] = field(default_factory=set)
    #: Per-shift multiplier on fairness credit (1.0 = normal share of the duty,
    #: 0 = never volunteered into the rotation for it).
    shift_weights: dict[str, float] = field(default_factory=dict)
    notes: str = ""

    def has_skills(self, required: Iterable[str]) -> bool:
        return set(required).issubset(self.skills)

    def is_unavailable(self, day: dt.date) -> str | None:
        for block in self.unavailable:
            if block.covers(day):
                return block.reason
        return None

    def admin_sessions(self, day: dt.date, holidays: set[dt.date]) -> list["AdminBlock"]:
        return [a for a in self.admin if a.recurrence.matches(day, holidays)]


@dataclass
class AdminBlock:
    """Protected non-clinical time (medical director, research, education)."""

    label: str
    session: str = "full"
    recurrence: Recurrence = field(default_factory=Recurrence)
    #: Soft blocks may be broken by the solver when coverage would otherwise fail.
    hard: bool = True


# ---------------------------------------------------------------------------
# duties
# ---------------------------------------------------------------------------


@dataclass
class ShiftDef:
    """A kind of work that must be staffed (clinic block, cath lab, echo reads)."""

    id: str
    label: str
    location: str = "hospital"  # hospital | clinic | remote | admin
    session: str = "full"
    #: Ranked tier: 1 = specialised/procedural (filled first), 2 = core coverage,
    #: 3 = residual clinic fill. Lower numbers are placed first.
    tier: int = 2
    required_skills: list[str] = field(default_factory=list)
    #: Any-of skill groups: physician must have >= 1 skill from each group.
    any_skills: list[list[str]] = field(default_factory=list)
    specialties: list[str] = field(default_factory=list)
    eligible: list[str] = field(default_factory=list)  # explicit allow-list
    min_count: int = 1
    max_count: int | None = None
    recurrence: Recurrence = field(default_factory=Recurrence)
    #: Group used when measuring equity ("cath", "weekend_call", "echo"...).
    fairness_group: str = ""
    #: Credit applied to the fairness ledger for one assignment.
    credit: float = 1.0
    #: Effort consumed out of a physician's daily clinical capacity (1.0/day).
    effort: float = 1.0
    #: Keep the same physician on this duty for a whole block of days.
    block_days: int = 1
    #: Prefer, but do not require, continuity with the previous period.
    tags: set[str] = field(default_factory=set)
    hard: bool = True  # unfilled hard shift = violation; soft = best effort

    def fairness_key(self) -> str:
        return self.fairness_group or self.id


@dataclass
class DayRule:
    """A census constraint evaluated per day across the whole service line.

    Example: "no more than 2 interventional cardiologists in the hospital",
    "at least 4 cardiologists in clinic on weekdays".
    """

    id: str
    description: str = ""
    locations: list[str] = field(default_factory=list)
    #: Limit the rule to certain sessions, e.g. ["full", "am", "pm"] to count
    #: physical presence only and ignore background reading lists.
    sessions: list[str] = field(default_factory=list)
    shift_ids: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)      # physician must have all
    any_skills: list[str] = field(default_factory=list)  # physician must have one
    specialties: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    physicians: list[str] = field(default_factory=list)
    min_count: int | None = None
    max_count: int | None = None
    #: distinct=True counts physicians; False counts assignments.
    distinct: bool = True
    recurrence: Recurrence = field(default_factory=Recurrence)
    severity: str = "hard"  # hard | soft
    weight: float = 1.0

    def matches_assignment(self, shift: ShiftDef, doc: Physician) -> bool:
        if self.locations and shift.location not in self.locations:
            return False
        if self.sessions and shift.session not in self.sessions:
            return False
        if self.shift_ids and shift.id not in self.shift_ids:
            return False
        if self.specialties and doc.specialty not in self.specialties:
            return False
        if self.skills and not doc.has_skills(self.skills):
            return False
        if self.any_skills and not (set(self.any_skills) & doc.skills):
            return False
        if self.tags and not (set(self.tags) & (doc.tags | shift.tags)):
            return False
        if self.physicians and doc.id not in self.physicians:
            return False
        return True


@dataclass
class Request:
    """A physician-submitted preference or hard commitment."""

    physician_id: str
    kind: str  # off_hard | off_soft | on_soft | on_hard | avoid_shift | prefer_shift
    start: dt.date
    end: dt.date
    shift_id: str | None = None
    weight: float = 1.0
    note: str = ""

    def covers(self, day: dt.date) -> bool:
        return self.start <= day <= self.end


# ---------------------------------------------------------------------------
# schedule output
# ---------------------------------------------------------------------------


@dataclass
class Slot:
    """One staffing need on one day (a shift may need several)."""

    date: dt.date
    shift_id: str
    index: int
    required: bool = True

    @property
    def key(self) -> tuple[dt.date, str, int]:
        return (self.date, self.shift_id, self.index)


@dataclass
class Assignment:
    date: dt.date
    shift_id: str
    physician_id: str
    index: int = 0
    reason: str = ""  # rotation | request | locked | backfill | repair


@dataclass
class Violation:
    date: dt.date | None
    kind: str
    severity: str
    message: str
    shift_id: str | None = None
    physician_id: str | None = None


@dataclass
class ServiceLine:
    name: str
    start: dt.date
    end: dt.date
    physicians: list[Physician]
    shifts: list[ShiftDef]
    rules: list[DayRule]
    requests: list[Request] = field(default_factory=list)
    holidays: set[dt.date] = field(default_factory=set)
    locked: list[Assignment] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.physician_by_id = {p.id: p for p in self.physicians}
        self.shift_by_id = {s.id: s for s in self.shifts}

    def days(self) -> list[dt.date]:
        out, day = [], self.start
        while day <= self.end:
            out.append(day)
            day += dt.timedelta(days=1)
        return out
