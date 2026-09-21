"""Post-hoc rule checking: everything the solver promised, verified independently."""
from __future__ import annotations

import datetime as dt

from .models import DayRule, ServiceLine, Slot, Violation, sessions_overlap
from .schedule import Schedule


def rule_applies(rule: DayRule, day: dt.date, line: ServiceLine) -> bool:
    return rule.recurrence.matches(day, line.holidays)


def rule_count(rule: DayRule, day: dt.date, sched: Schedule) -> int:
    line = sched.line
    hits = [
        a
        for a in sched.by_day[day]
        if rule.matches_assignment(line.shift_by_id[a.shift_id], line.physician_by_id[a.physician_id])
    ]
    if rule.distinct:
        return len({a.physician_id for a in hits})
    return len(hits)


def day_rule_violations(sched: Schedule, day: dt.date) -> list[Violation]:
    line = sched.line
    out: list[Violation] = []
    for rule in line.rules:
        if not rule_applies(rule, day, line):
            continue
        count = rule_count(rule, day, sched)
        if rule.min_count is not None and count < rule.min_count:
            out.append(
                Violation(day, "rule_min", rule.severity,
                          f"{rule.id}: {count} of required minimum {rule.min_count}"
                          f" ({rule.description})" if rule.description else
                          f"{rule.id}: {count} of required minimum {rule.min_count}")
            )
        if rule.max_count is not None and count > rule.max_count:
            out.append(
                Violation(day, "rule_max", rule.severity,
                          f"{rule.id}: {count} exceeds maximum {rule.max_count}"
                          + (f" ({rule.description})" if rule.description else ""))
            )
    return out


def would_break_hard_max(sched: Schedule, day: dt.date, shift_id: str, physician_id: str) -> bool:
    """Cheap look-ahead: does adding this assignment push a hard max rule over?"""
    line = sched.line
    shift, doc = line.shift_by_id[shift_id], line.physician_by_id[physician_id]
    for rule in line.rules:
        if rule.max_count is None or rule.severity != "hard":
            continue
        if not rule.matches_assignment(shift, doc):
            continue
        if not rule_applies(rule, day, line):
            continue
        count = rule_count(rule, day, sched)
        if rule.distinct and physician_id in {
            a.physician_id
            for a in sched.by_day[day]
            if rule.matches_assignment(line.shift_by_id[a.shift_id],
                                       line.physician_by_id[a.physician_id])
        }:
            continue  # already counted
        if count + 1 > rule.max_count:
            return True
    return False


def soft_rule_pressure(sched: Schedule, day: dt.date, shift_id: str, physician_id: str) -> float:
    """Penalty (>=0) for how much a candidate assignment strains soft rules."""
    line = sched.line
    shift, doc = line.shift_by_id[shift_id], line.physician_by_id[physician_id]
    penalty = 0.0
    for rule in line.rules:
        if rule.severity != "soft" or not rule_applies(rule, day, line):
            continue
        if not rule.matches_assignment(shift, doc):
            continue
        count = rule_count(rule, day, sched)
        if rule.max_count is not None and count + 1 > rule.max_count:
            penalty += rule.weight
        if rule.min_count is not None and count < rule.min_count:
            penalty -= rule.weight  # helping a shortfall is a bonus
    return penalty


def validate_schedule(sched: Schedule, slots: list[Slot]) -> list[Violation]:
    line = sched.line
    out: list[Violation] = []

    # 1. unfilled required slots. Optional positions (the headroom between a
    # duty's minimum and maximum) are spare capacity, not a shortfall.
    for slot in slots:
        if slot.key in sched.by_slot or not slot.required:
            continue
        shift = line.shift_by_id[slot.shift_id]
        out.append(Violation(slot.date, "unfilled", "hard" if shift.hard else "soft",
                             f"{shift.label} has no physician assigned", shift_id=shift.id))

    # 2. per-physician integrity
    for doc in line.physicians:
        for day in line.days():
            todays = sched.by_doc_day[(doc.id, day)]
            if not todays:
                continue
            reason = doc.is_unavailable(day)
            if reason:
                out.append(Violation(day, "unavailable", "hard",
                                     f"{doc.name} is scheduled while {reason}",
                                     physician_id=doc.id))
            for i, a in enumerate(todays):
                sa = line.shift_by_id[a.shift_id]
                for b in todays[i + 1:]:
                    sb = line.shift_by_id[b.shift_id]
                    if sessions_overlap(sa.session, sb.session):
                        out.append(Violation(day, "double_booked", "hard",
                                             f"{doc.name} is booked for {sa.label} and {sb.label}",
                                             physician_id=doc.id))
            effort = sched.effort_on(doc.id, day)
            if effort > 1.0 + 1e-9:
                out.append(Violation(day, "over_capacity", "hard",
                                     f"{doc.name} is scheduled {effort:.1f} days of work in one day",
                                     physician_id=doc.id))
            for block in doc.admin_sessions(day, line.holidays):
                if not block.hard:
                    continue
                for a in todays:
                    if sessions_overlap(line.shift_by_id[a.shift_id].session, block.session):
                        out.append(Violation(day, "admin_conflict", "hard",
                                             f"{doc.name}: {line.shift_by_id[a.shift_id].label}"
                                             f" collides with protected {block.label}",
                                             physician_id=doc.id))
        # weekly / consecutive caps
        if doc.max_clinical_days_per_week is not None:
            week_start = line.start - dt.timedelta(days=line.start.weekday())
            while week_start <= line.end:
                worked = sum(
                    1
                    for i in range(7)
                    if line.start <= week_start + dt.timedelta(days=i) <= line.end
                    and sched.effort_on(doc.id, week_start + dt.timedelta(days=i)) > 0
                )
                if worked > doc.max_clinical_days_per_week:
                    out.append(Violation(week_start, "weekly_cap", "soft",
                                         f"{doc.name} works {worked} days in the week of"
                                         f" {week_start:%Y-%m-%d} (cap"
                                         f" {doc.max_clinical_days_per_week:g})",
                                         physician_id=doc.id))
                week_start += dt.timedelta(days=7)
        run = 0
        for day in line.days():
            run = run + 1 if sched.effort_on(doc.id, day) > 0 else 0
            if run == doc.max_consecutive_clinical_days + 1:
                out.append(Violation(day, "consecutive_cap", "soft",
                                     f"{doc.name} exceeds {doc.max_consecutive_clinical_days}"
                                     " consecutive clinical days",
                                     physician_id=doc.id))

    # 3. hard time-off requests
    for req in line.requests:
        if req.kind != "off_hard":
            continue
        for day in line.days():
            if req.covers(day) and sched.by_doc_day[(req.physician_id, day)]:
                out.append(Violation(day, "request_conflict", "hard",
                                     f"{line.physician_by_id[req.physician_id].name} has approved"
                                     f" time off ({req.note or 'requested'}) but is scheduled",
                                     physician_id=req.physician_id))

    # 4. census rules
    for day in line.days():
        out.extend(day_rule_violations(sched, day))

    out.sort(key=lambda v: (v.severity != "hard", v.date or dt.date.min, v.kind))
    return out
