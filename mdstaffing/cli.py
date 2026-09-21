"""Command line interface: ``python -m mdstaffing ...``"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import report
from .config import load_service_line
from .fairness import Ledger
from .models import ConfigError, parse_date
from .solver import Scheduler


def _load(args) -> tuple:
    line = load_service_line(args.config)
    if getattr(args, "start", None):
        line.start = parse_date(args.start)
    if getattr(args, "end", None):
        line.end = parse_date(args.end)
    ledger = Ledger.load(getattr(args, "ledger", None))
    return line, ledger


def cmd_validate(args) -> int:
    line = load_service_line(args.config)
    print(f"OK: {line.name} — {len(line.physicians)} physicians, {len(line.shifts)} duties, "
          f"{len(line.rules)} census rules, {len(line.requests)} requests.")
    return 0


def cmd_build(args) -> int:
    line, ledger = _load(args)
    scheduler = Scheduler(line, ledger, seed=args.seed)
    result = scheduler.solve()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report.write_csv(report.coverage_grid(result), out / "coverage.csv")
    report.write_csv(report.physician_grid(result), out / "by_physician.csv")
    report.write_csv(report.equity_report(result), out / "equity.csv")
    report.write_csv(report.violations_rows(result), out / "rule_check.csv")
    report.write_html(result, out / "schedule.html")
    report.write_ics(result, out / "schedule.ics")
    if args.ledger_out:
        result.ledger.save(args.ledger_out)

    print(report.text_summary(result))
    print(f"\nWrote {out}/schedule.html, coverage.csv, by_physician.csv, equity.csv, "
          f"rule_check.csv, schedule.ics")
    if args.strict and result.hard_violations:
        return 2
    return 0


def cmd_explain(args) -> int:
    """Why is (or isn't) a physician available for a duty on a date?"""
    line, ledger = _load(args)
    scheduler = Scheduler(line, ledger, seed=args.seed)
    result = scheduler.solve()
    day = parse_date(args.date)
    shift = line.shift_by_id.get(args.shift)
    if shift is None:
        print(f"unknown duty {args.shift!r}; known: {', '.join(sorted(line.shift_by_id))}")
        return 1
    if not shift.recurrence.matches(day, line.holidays):
        print(f"{shift.label} does not occur on {day:%a %Y-%m-%d}.")
        return 0

    assigned = [a.physician_id for a in result.schedule.by_shift_day[(day, shift.id)]]
    print(f"{shift.label} on {day:%a %Y-%m-%d} — assigned: "
          f"{', '.join(line.physician_by_id[p].name for p in assigned) or '(nobody)'}\n")
    print(f"{'Physician':24} {'Eligible':9} {'Credit':>7}  Status")
    for doc in sorted(line.physicians, key=lambda p: p.name):
        eligible = scheduler.is_eligible(doc, shift)
        credit = result.ledger.get(shift.fairness_key(), doc.id)
        if doc.id in assigned:
            status = "ASSIGNED"
        elif not eligible:
            missing = set(shift.required_skills) - doc.skills
            bits = []
            if shift.id in doc.excluded_shifts:
                bits.append("excluded from this duty")
            if doc.shift_weights.get(shift.id, 1.0) <= 0:
                bits.append("opted out of this duty")
            if missing:
                bits.append("lacks " + ", ".join(sorted(missing)))
            if shift.eligible and doc.id not in shift.eligible:
                bits.append("not on the allow-list")
            if shift.specialties and doc.specialty not in shift.specialties:
                bits.append(f"specialty is {doc.specialty}")
            missing_tags = set(shift.required_tags) - doc.tags
            if missing_tags:
                bits.append("not in group " + ", ".join(sorted(missing_tags)))
            if shift.any_tags and not (set(shift.any_tags) & doc.tags):
                bits.append("not in " + "/".join(sorted(shift.any_tags)))
            status = "; ".join(bits) or "not credentialed"
        else:
            reasons = scheduler.block_reasons(result.schedule, doc, shift, day, collect=True)
            status = "; ".join(reasons) if reasons else "available — passed over by rotation"
        print(f"{doc.name:24} {'yes' if eligible else 'no':9} {credit:7.1f}  {status}")
    return 0


def cmd_ledger(args) -> int:
    ledger = Ledger.load(args.ledger)
    if not ledger.credits:
        print("ledger is empty")
        return 0
    for group in sorted(ledger.credits):
        print(f"\n[{group}]")
        for pid, credit in sorted(ledger.credits[group].items(), key=lambda t: -t[1]):
            last = ledger.last_worked.get(group, {}).get(pid, "-")
            print(f"  {pid:20} {credit:7.1f}   last: {last}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mdstaffing",
                                description="Physician scheduling for a multi-site service line.")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("config", help="path to the service-line YAML config")
        sp.add_argument("--start", help="override period start (YYYY-MM-DD)")
        sp.add_argument("--end", help="override period end (YYYY-MM-DD)")
        sp.add_argument("--ledger", help="carry-in fairness ledger JSON from prior periods")
        sp.add_argument("--seed", type=int, default=0, help="tie-break seed for reproducibility")

    sp = sub.add_parser("validate", help="check the config without scheduling")
    sp.add_argument("config")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("build", help="generate the schedule and reports")
    common(sp)
    sp.add_argument("--out", default="out", help="output directory (default: out)")
    sp.add_argument("--ledger-out", help="write the updated fairness ledger here")
    sp.add_argument("--strict", action="store_true",
                    help="exit non-zero if any hard rule is violated")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("explain", help="show who can cover a duty on a date, and why not")
    common(sp)
    sp.add_argument("--date", required=True)
    sp.add_argument("--shift", required=True)
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("ledger", help="print a fairness ledger")
    sp.add_argument("ledger")
    sp.set_defaults(func=cmd_ledger)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
