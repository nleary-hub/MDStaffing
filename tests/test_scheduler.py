"""Tests for the scheduling engine.

The synthetic fixtures are deliberately tiny so that each rule can be checked in
isolation; the last section runs the real service-line config end to end.
"""
from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path

import pytest

from mdstaffing import Ledger, load_service_line, solve
from mdstaffing.models import ConfigError, NthWeekday, Recurrence

CONFIG = Path(__file__).resolve().parents[1] / "config" / "service_line.yaml"


def build(tmp_path: Path, body: str, name: str = "t.yaml"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return load_service_line(path)


BASE = textwrap.dedent("""
    name: test
    period: {start: 2026-03-02, end: 2026-03-15}
    physicians:
      - {id: a, name: A, skills: [cath, echo]}
      - {id: b, name: B, skills: [cath, echo]}
      - {id: c, name: C, skills: [echo]}
    shifts:
      - id: LAB
        label: Lab
        count: 1
        required_skills: [cath]
        when: {weekdays: [mon, tue, wed, thu, fri]}
    rules: []
""")


# --- recurrence -------------------------------------------------------------

def test_recurrence_weekdays():
    r = Recurrence(weekdays=[0, 2])
    assert r.matches(dt.date(2026, 3, 2))      # Monday
    assert not r.matches(dt.date(2026, 3, 3))  # Tuesday
    assert r.matches(dt.date(2026, 3, 4))      # Wednesday


def test_recurrence_nth_weekday_of_month():
    r = Recurrence(nth_weekdays=[NthWeekday(2, 2)], weekdays=[2])  # 2nd Wednesday
    assert r.matches(dt.date(2026, 3, 11))
    assert not r.matches(dt.date(2026, 3, 4))
    assert not r.matches(dt.date(2026, 3, 18))


def test_recurrence_last_weekday_of_month():
    r = Recurrence(nth_weekdays=[NthWeekday(-1, 4)], weekdays=[4])  # last Friday
    assert r.matches(dt.date(2026, 3, 27))
    assert not r.matches(dt.date(2026, 3, 20))


def test_recurrence_every_other_week():
    r = Recurrence(weekdays=[0], interval_weeks=2, anchor=dt.date(2026, 3, 2))
    assert r.matches(dt.date(2026, 3, 2))
    assert not r.matches(dt.date(2026, 3, 9))
    assert r.matches(dt.date(2026, 3, 16))


def test_recurrence_holidays_and_exclusions():
    holiday = dt.date(2026, 3, 9)
    r = Recurrence(weekdays=[0], exclude_dates=[dt.date(2026, 3, 16)])
    assert not r.matches(holiday, {holiday})
    assert Recurrence(weekdays=[0], include_holidays=True).matches(holiday, {holiday})
    assert not r.matches(dt.date(2026, 3, 16))


def test_explicit_dates_override_weekday_filter():
    r = Recurrence(weekdays=[0], dates=[dt.date(2026, 3, 7)])  # a Saturday
    assert r.matches(dt.date(2026, 3, 7))


# --- config validation ------------------------------------------------------

def test_unknown_skill_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="required skill"):
        build(tmp_path, BASE.replace("required_skills: [cath]", "required_skills: [tavr]"))


def test_rule_without_bounds_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="needs a min"):
        build(tmp_path, BASE.replace("rules: []", "rules:\n      - {id: r1, locations: [clinic]}"))


def test_unknown_physician_in_request_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown physician"):
        build(tmp_path, BASE + "\nrequests:\n  - {physician: zz, kind: off_hard, date: 2026-03-02}")


def test_backwards_vacation_is_rejected(tmp_path):
    body = BASE.replace(
        "- {id: c, name: C, skills: [echo]}",
        "- id: c\n    name: C\n    skills: [echo]\n"
        "    unavailable: [{start: 2026-03-10, end: 2026-03-02}]",
    )
    with pytest.raises(ConfigError, match="ends before"):
        build(tmp_path, body)


# --- hard constraints -------------------------------------------------------

def test_only_credentialed_physicians_get_the_duty(tmp_path):
    result = solve(build(tmp_path, BASE))
    assert result.schedule.assignments
    assert {a.physician_id for a in result.schedule.assignments} == {"a", "b"}
    assert not result.hard_violations


def test_vacation_is_never_overridden(tmp_path):
    body = BASE.replace(
        "- {id: a, name: A, skills: [cath, echo]}",
        "- id: a\n    name: A\n    skills: [cath, echo]\n"
        "    unavailable: [{start: 2026-03-02, end: 2026-03-15}]",
    )
    result = solve(build(tmp_path, body))
    assert all(a.physician_id != "a" for a in result.schedule.assignments)
    assert not result.hard_violations


def test_approved_time_off_is_never_overridden(tmp_path):
    body = BASE + textwrap.dedent("""
        requests:
          - {physician: a, kind: off_hard, start: 2026-03-02, end: 2026-03-15}
          - {physician: b, kind: off_hard, start: 2026-03-02, end: 2026-03-15}
    """)
    result = solve(build(tmp_path, body))
    assert not result.schedule.assignments          # nobody left who can run the lab
    assert result.hard_violations                   # and the gap is reported
    assert all(v.kind == "unfilled" for v in result.hard_violations)


def test_hard_maximum_census_rule_is_enforced(tmp_path):
    body = """
        name: cap
        period: {start: 2026-03-02, end: 2026-03-06}
        physicians:
          - {id: a, name: A, skills: [cath]}
          - {id: b, name: B, skills: [cath]}
          - {id: c, name: C, skills: [cath]}
        shifts:
          - {id: LAB, label: Lab, count: 3, required_skills: [cath], location: hospital,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
        rules:
          - {id: cap, locations: [hospital], max: 2, severity: hard,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    for day in result.schedule.line.days():
        assert len(result.schedule.by_day[day]) <= 2
    assert any(v.kind == "unfilled" for v in result.hard_violations)
    assert not any(v.kind == "rule_max" for v in result.violations)


def test_minimum_census_rule_pulls_in_extra_physicians(tmp_path):
    body = """
        name: floor
        period: {start: 2026-03-02, end: 2026-03-06}
        physicians:
          - {id: a, name: A}
          - {id: b, name: B}
          - {id: c, name: C}
        shifts:
          - {id: CLINIC, label: Clinic, count: 1, max_count: 3, location: clinic, tier: 3,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
        rules:
          - {id: floor, locations: [clinic], min: 3, severity: soft,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    for day in result.schedule.line.days():
        if day.weekday() < 5:
            assert len(result.schedule.by_day[day]) == 3
    assert not [v for v in result.violations if v.kind == "rule_min"]


def test_no_double_booking_across_sessions(tmp_path):
    body = """
        name: sessions
        period: {start: 2026-03-02, end: 2026-03-06}
        physicians:
          - {id: a, name: A, skills: [echo]}
        shifts:
          - {id: AM, label: Morning, session: am, count: 1,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
          - {id: PM, label: Afternoon, session: pm, count: 1,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
          - {id: FULL, label: All day, session: full, count: 1,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
          - {id: READ, label: Reads, session: background, count: 1, required_skills: [echo],
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    monday = dt.date(2026, 3, 2)
    todays = {a.shift_id for a in result.schedule.by_doc_day[("a", monday)]}
    # one clinical session (or the two halves) plus the background read at most
    assert "READ" in todays
    assert not ({"AM", "FULL"} <= todays) and not ({"PM", "FULL"} <= todays)
    assert result.schedule.effort_on("a", monday) <= 1.0
    assert not [v for v in result.violations if v.kind in ("double_booked", "over_capacity")]


def test_protected_admin_time_is_respected(tmp_path):
    body = """
        name: admin
        period: {start: 2026-03-02, end: 2026-03-06}
        physicians:
          - id: a
            name: A
            admin: [{label: Director, session: full, when: {weekdays: [wed]}}]
          - {id: b, name: B}
        shifts:
          - {id: SVC, label: Service, count: 1,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    wed = dt.date(2026, 3, 4)
    assert not result.schedule.by_doc_day[("a", wed)]
    assert not result.hard_violations


def test_weekly_day_cap_is_respected(tmp_path):
    body = """
        name: cap
        period: {start: 2026-03-02, end: 2026-03-06}
        physicians:
          - {id: a, name: A, max_clinical_days_per_week: 2}
          - {id: b, name: B}
        shifts:
          - {id: SVC, label: Service, count: 1,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
          - {id: READ, label: Reads, session: background, count: 1,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    worked = sum(1 for d in result.schedule.line.days()
                 if result.schedule.effort_on("a", d) > 0)
    assert worked <= 2
    assert not [v for v in result.violations if v.kind == "weekly_cap"]


def test_locked_assignment_is_kept(tmp_path):
    body = BASE + "\nlocked:\n  - {date: 2026-03-03, shift: LAB, physician: b}"
    result = solve(build(tmp_path, body))
    assert result.schedule.by_slot[(dt.date(2026, 3, 3), "LAB", 0)].physician_id == "b"


def test_tag_restricted_duty_only_goes_to_that_group(tmp_path):
    """Call pools are group membership, not credentialing."""
    body = """
        name: tags
        period: {start: 2026-03-02, end: 2026-03-08}
        physicians:
          - {id: a, name: A, tags: [invasive], skills: [pci]}
          - {id: b, name: B, tags: [invasive], skills: [pci]}
          - {id: c, name: C, tags: [non_invasive]}
          - {id: d, name: D, tags: [non_invasive]}
        shifts:
          - {id: STEMI_CALL, label: STEMI call, session: background, count: 1,
             required_tags: [invasive],
             when: {weekdays: [mon, tue, wed, thu, fri, sat, sun]}}
          - {id: GEN_CALL, label: General call, session: background, count: 1,
             required_tags: [non_invasive],
             when: {weekdays: [mon, tue, wed, thu, fri, sat, sun]}}
    """
    result = solve(build(tmp_path, body))
    stemi = {a.physician_id for a in result.schedule.assignments
             if a.shift_id == "STEMI_CALL"}
    general = {a.physician_id for a in result.schedule.assignments
               if a.shift_id == "GEN_CALL"}
    assert stemi == {"a", "b"}
    assert general == {"c", "d"}
    assert not result.hard_violations


def test_unknown_required_tag_is_rejected(tmp_path):
    body = BASE.replace("required_skills: [cath]", "required_tags: [invasive]")
    with pytest.raises(ConfigError, match="required tag"):
        build(tmp_path, body)


def test_single_operator_duty_is_flagged_when_they_are_away(tmp_path):
    """A one-deep procedure (e.g. the only TAVR operator) surfaces as a gap."""
    body = """
        name: solo
        period: {start: 2026-03-02, end: 2026-03-06}
        physicians:
          - id: a
            name: A
            skills: [structural, pci]
            unavailable: [{start: 2026-03-04, end: 2026-03-04}]
          - {id: b, name: B, skills: [pci]}
        shifts:
          - {id: TAVR, label: TAVR, count: 1, required_skills: [structural],
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    gaps = [v for v in result.hard_violations if v.kind == "unfilled"]
    assert len(gaps) == 1
    assert gaps[0].date == dt.date(2026, 3, 4)


# --- fairness ---------------------------------------------------------------

def test_equal_physicians_share_a_duty_evenly(tmp_path):
    body = """
        name: fair
        period: {start: 2026-03-02, end: 2026-03-27}
        physicians:
          - {id: a, name: A, skills: [cath]}
          - {id: b, name: B, skills: [cath]}
          - {id: c, name: C, skills: [cath]}
          - {id: d, name: D, skills: [cath]}
        shifts:
          - {id: LAB, label: Lab, count: 1, required_skills: [cath], fairness_group: cath,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    counts = [len(result.schedule.by_doc[p]) for p in "abcd"]
    assert max(counts) - min(counts) <= 1


def test_part_time_physician_gets_a_proportional_share(tmp_path):
    body = """
        name: fte
        period: {start: 2026-03-02, end: 2026-04-24}
        physicians:
          - {id: a, name: A, skills: [cath]}
          - {id: b, name: B, skills: [cath]}
          - {id: c, name: C, skills: [cath], fte: 0.5}
        shifts:
          - {id: LAB, label: Lab, count: 1, required_skills: [cath], fairness_group: cath,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    full = (len(result.schedule.by_doc["a"]) + len(result.schedule.by_doc["b"])) / 2
    assert 0.35 * full <= len(result.schedule.by_doc["c"]) <= 0.65 * full


def test_vacation_reduces_expected_share_not_future_debt(tmp_path):
    """A physician away half the period is not punished with a double load later."""
    body = """
        name: avail
        period: {start: 2026-03-02, end: 2026-03-27}
        physicians:
          - {id: a, name: A, skills: [cath]}
          - {id: b, name: B, skills: [cath]}
          - id: c
            name: C
            skills: [cath]
            unavailable: [{start: 2026-03-02, end: 2026-03-13}]
        shifts:
          - {id: LAB, label: Lab, count: 1, required_skills: [cath], fairness_group: cath,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    second_half = [a for a in result.schedule.by_doc["c"] if a.date >= dt.date(2026, 3, 16)]
    # 10 remaining working days, three physicians -> not more than a fair catch-up
    assert len(second_half) <= 5


def test_carry_in_ledger_shifts_the_rotation(tmp_path):
    body = """
        name: carry
        period: {start: 2026-03-02, end: 2026-03-06}
        physicians:
          - {id: a, name: A, skills: [cath]}
          - {id: b, name: B, skills: [cath]}
        shifts:
          - {id: LAB, label: Lab, count: 1, required_skills: [cath], fairness_group: cath,
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    line = build(tmp_path, body)
    ledger = Ledger()
    ledger.add("cath", "a", 20.0)
    result = solve(line, ledger)
    assert result.schedule.by_slot[(dt.date(2026, 3, 2), "LAB", 0)].physician_id == "b"
    assert len(result.schedule.by_doc["b"]) > len(result.schedule.by_doc["a"])


def test_opting_out_of_a_duty_is_honoured(tmp_path):
    body = """
        name: optout
        period: {start: 2026-03-02, end: 2026-03-13}
        physicians:
          - {id: a, name: A, skills: [cath]}
          - {id: b, name: B, skills: [cath], shift_weights: {LAB: 0}}
        shifts:
          - {id: LAB, label: Lab, count: 1, required_skills: [cath],
             when: {weekdays: [mon, tue, wed, thu, fri]}}
    """
    result = solve(build(tmp_path, body))
    assert all(a.physician_id == "a" for a in result.schedule.assignments)


def test_block_service_stays_with_one_physician(tmp_path):
    body = """
        name: block
        period: {start: 2026-03-02, end: 2026-03-29}
        physicians:
          - {id: a, name: A}
          - {id: b, name: B}
          - {id: c, name: C}
          - {id: d, name: D}
        shifts:
          - {id: SVC, label: Service, count: 1, block_days: 7,
             when: {weekdays: [mon, tue, wed, thu, fri, sat, sun]}}
    """
    result = solve(build(tmp_path, body))
    week_one = {result.schedule.by_slot[(dt.date(2026, 3, d), "SVC", 0)].physician_id
                for d in range(2, 9)}
    assert len(week_one) == 1


def test_scheduling_requests_are_weighed(tmp_path):
    body = """
        name: requests
        period: {start: 2026-03-02, end: 2026-03-06}
        physicians:
          - {id: a, name: A, skills: [cath]}
          - {id: b, name: B, skills: [cath]}
        shifts:
          - {id: LAB, label: Lab, count: 1, required_skills: [cath],
             when: {weekdays: [mon, tue, wed, thu, fri]}}
        requests:
          - {physician: a, kind: off_soft, start: 2026-03-02, end: 2026-03-04, weight: 5}
    """
    result = solve(build(tmp_path, body))
    early = [a.physician_id for a in result.schedule.assignments
             if a.date <= dt.date(2026, 3, 4)]
    assert early == ["b", "b", "b"]


# --- determinism & end to end ----------------------------------------------

def test_same_seed_gives_the_same_schedule(tmp_path):
    line1, line2 = build(tmp_path, BASE, "a.yaml"), build(tmp_path, BASE, "b.yaml")
    keys = lambda r: [(a.date, a.shift_id, a.physician_id) for a in r.schedule.assignments]
    assert keys(solve(line1, seed=7)) == keys(solve(line2, seed=7))


def test_real_service_line_config_solves_cleanly():
    result = solve(load_service_line(CONFIG))
    assert not result.hard_violations, [v.message for v in result.hard_violations]
    assert len(result.schedule.assignments) > 400


def test_real_service_line_respects_every_vacation():
    line = load_service_line(CONFIG)
    result = solve(line)
    for doc in line.physicians:
        for day in line.days():
            if doc.is_unavailable(day):
                assert not result.schedule.by_doc_day[(doc.id, day)], (doc.id, day)


def test_real_service_line_procedural_duties_are_credentialed():
    line = load_service_line(CONFIG)
    result = solve(line)
    for a in result.schedule.assignments:
        shift = line.shift_by_id[a.shift_id]
        doc = line.physician_by_id[a.physician_id]
        assert doc.has_skills(shift.required_skills), (a.shift_id, a.physician_id)


def test_ledger_round_trip(tmp_path):
    result = solve(load_service_line(CONFIG))
    path = tmp_path / "ledger.json"
    result.ledger.save(path)
    again = Ledger.load(path)
    assert again.credits == {k: v for k, v in result.ledger.credits.items()}
