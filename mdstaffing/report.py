"""Outputs: grid CSV, per-physician CSV, equity report, ICS feed, HTML view."""
from __future__ import annotations

import csv
import datetime as dt
import html
from collections import defaultdict
from pathlib import Path

from .fairness import debt
from .solver import SolveResult

SEV_ORDER = {"hard": 0, "soft": 1}


def _docs_for(result: SolveResult, day: dt.date, shift_id: str) -> str:
    names = [
        result.schedule.line.physician_by_id[a.physician_id].name
        for a in sorted(result.schedule.by_shift_day[(day, shift_id)], key=lambda a: a.index)
    ]
    return "; ".join(names)


def coverage_grid(result: SolveResult) -> list[list[str]]:
    """Rows = duties, columns = days. The view a service-line chief reads."""
    line = result.schedule.line
    days = line.days()
    header = ["Duty", "Location", "Session"] + [f"{d:%a %m/%d}" for d in days]
    rows = [header]
    for shift in sorted(line.shifts, key=lambda s: (s.tier, s.location, s.id)):
        occ = {d for d in days if shift.recurrence.matches(d, line.holidays)}
        if not occ:
            continue
        row = [shift.label, shift.location, shift.session]
        for d in days:
            row.append(_docs_for(result, d, shift.id) if d in occ else "")
        rows.append(row)
    return rows


def physician_grid(result: SolveResult) -> list[list[str]]:
    """Rows = physicians, columns = days; the view each physician reads."""
    line = result.schedule.line
    days = line.days()
    rows = [["Physician", "Specialty", "FTE"] + [f"{d:%a %m/%d}" for d in days]]
    for doc in sorted(line.physicians, key=lambda p: (p.specialty, p.name)):
        row = [doc.name, doc.specialty, f"{doc.fte:g}"]
        for d in days:
            reason = doc.is_unavailable(d)
            todays = result.schedule.by_doc_day[(doc.id, d)]
            if todays:
                row.append(" + ".join(
                    sorted(line.shift_by_id[a.shift_id].label for a in todays)))
            elif reason:
                row.append(reason.upper())
            else:
                admin = doc.admin_sessions(d, line.holidays)
                if admin:
                    row.append(admin[0].label)
                elif d.weekday() >= 5 or d in line.holidays:
                    row.append("")
                else:
                    row.append("available")
        rows.append(row)
    return rows


def equity_report(result: SolveResult) -> list[list[str]]:
    """Assigned vs. expected share for every duty group, per physician."""
    line = result.schedule.line
    sched = result.schedule
    groups: dict[str, set[str]] = defaultdict(set)
    for shift in line.shifts:
        groups[shift.fairness_key()].add(shift.id)

    period_credit: dict[tuple[str, str], float] = defaultdict(float)
    for a in sched.assignments:
        shift = line.shift_by_id[a.shift_id]
        period_credit[(shift.fairness_key(), a.physician_id)] += shift.credit
    for a in sched.assignments:
        if a.date.weekday() >= 5:
            period_credit[("_weekend", a.physician_id)] += line.shift_by_id[a.shift_id].credit
        if a.date in line.holidays:
            period_credit[("_holiday", a.physician_id)] += line.shift_by_id[a.shift_id].credit

    rows = [["Duty group", "Physician", "Assigned (period)", "Expected (period)",
             "Delta", "Cumulative credit"]]
    for group in sorted(set(groups) | {"_weekend", "_holiday"}):
        shares = result.shares.get(group, {})
        assigned = {
            pid: period_credit[(group, pid)]
            for pid in shares or {d.id for d in line.physicians}
        }
        grand = sum(assigned.values())
        if grand <= 0:
            continue
        if not shares:  # fall back to a flat FTE split
            fte = {pid: line.physician_by_id[pid].fte for pid in assigned}
            total = sum(fte.values()) or 1.0
            shares = {pid: v / total for pid, v in fte.items()}
        for pid, credit in sorted(assigned.items(), key=lambda t: -t[1]):
            expected = grand * shares.get(pid, 0.0)
            if credit == 0 and expected < 0.05:
                continue
            rows.append([
                group, line.physician_by_id[pid].name, f"{credit:g}",
                f"{expected:.1f}", f"{credit - expected:+.1f}",
                f"{result.ledger.get(group, pid):g}",
            ])
    return rows


def violations_rows(result: SolveResult) -> list[list[str]]:
    rows = [["Severity", "Date", "Kind", "Detail"]]
    for v in sorted(result.violations,
                    key=lambda v: (SEV_ORDER.get(v.severity, 2), v.date or dt.date.min)):
        rows.append([v.severity, v.date.isoformat() if v.date else "", v.kind, v.message])
    return rows


def write_csv(rows: list[list[str]], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)


def write_ics(result: SolveResult, path: str | Path, physician_id: str | None = None) -> None:
    line = result.schedule.line
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MDStaffing//Physician Schedule//EN"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for a in sorted(result.schedule.assignments, key=lambda a: (a.date, a.shift_id)):
        if physician_id and a.physician_id != physician_id:
            continue
        shift = line.shift_by_id[a.shift_id]
        doc = line.physician_by_id[a.physician_id]
        uid = f"{a.date:%Y%m%d}-{a.shift_id}-{a.index}-{a.physician_id}@mdstaffing"
        out += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{a.date:%Y%m%d}",
            f"DTEND;VALUE=DATE:{a.date + dt.timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{shift.label} - {doc.name}",
            f"LOCATION:{shift.location}",
            f"DESCRIPTION:{shift.session} session ({a.reason})",
            "END:VEVENT",
        ]
    out.append("END:VCALENDAR")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\r\n".join(out) + "\r\n")


def _table(rows: list[list[str]], cls: str = "") -> str:
    head = "".join(f"<th>{html.escape(c)}</th>" for c in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
        for r in rows[1:]
    )
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def write_html(result: SolveResult, path: str | Path) -> None:
    line = result.schedule.line
    hard = [v for v in result.violations if v.severity == "hard"]
    soft = [v for v in result.violations if v.severity == "soft"]
    banner = (
        f'<p class="ok">No hard rule violations. {len(soft)} soft item(s) to review.</p>'
        if not hard
        else f'<p class="bad">{len(hard)} hard violation(s) must be resolved before publishing.</p>'
    )
    doc_html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(line.name)} schedule</title><style>
body{{font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#14212e}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:26px 0 8px}}
table{{border-collapse:collapse;font-size:12px;margin-bottom:8px}}
th,td{{border:1px solid #d5dde5;padding:4px 7px;text-align:left;vertical-align:top}}
th{{background:#eef3f8;position:sticky;top:0}}
tr:nth-child(even) td{{background:#fafcfe}}
.ok{{color:#0a6b3d}} .bad{{color:#a1231d;font-weight:600}}
.wrap{{overflow-x:auto}} .meta{{color:#5b6b7c}}
</style></head><body>
<h1>{html.escape(line.name)}</h1>
<p class="meta">{line.start:%b %d, %Y} &ndash; {line.end:%b %d, %Y} &middot;
{len(line.physicians)} physicians &middot; {len(result.schedule.assignments)} assignments</p>
{banner}
<h2>Coverage by duty</h2><div class="wrap">{_table(coverage_grid(result))}</div>
<h2>Schedule by physician</h2><div class="wrap">{_table(physician_grid(result))}</div>
<h2>Equity</h2><div class="wrap">{_table(equity_report(result))}</div>
<h2>Rule check</h2><div class="wrap">{_table(violations_rows(result))}</div>
</body></html>"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(doc_html)


def text_summary(result: SolveResult) -> str:
    line = result.schedule.line
    hard = [v for v in result.violations if v.severity == "hard"]
    soft = [v for v in result.violations if v.severity == "soft"]
    lines = [
        f"{line.name}: {line.start} to {line.end}",
        f"  physicians      : {len(line.physicians)}",
        f"  duties defined  : {len(line.shifts)}",
        f"  slots required  : {sum(1 for s in result.slots if s.required)}",
        f"  assignments made: {len(result.schedule.assignments)}",
        f"  hard violations : {len(hard)}",
        f"  soft warnings   : {len(soft)}",
    ]
    for v in hard[:20]:
        lines.append(f"    HARD {v.date} {v.kind}: {v.message}")
    if len(hard) > 20:
        lines.append(f"    ... and {len(hard) - 20} more")
    return "\n".join(lines)


def schedule_json(result: SolveResult) -> dict:
    """Everything a viewer needs, in one serialisable structure."""
    line = result.schedule.line
    days = line.days()

    def group_of(doc) -> str:
        for tag in ("invasive", "ep", "non_invasive", "pulm"):
            if tag in doc.tags:
                return tag
        return doc.specialty

    equity: list[dict] = []
    for row in equity_report(result)[1:]:
        equity.append({
            "group": row[0], "physician": row[1], "assigned": float(row[2]),
            "expected": float(row[3]), "delta": float(row[4]),
        })

    return {
        "name": line.name,
        "start": line.start.isoformat(),
        "end": line.end.isoformat(),
        "days": [
            {
                "date": d.isoformat(),
                "weekday": d.strftime("%a"),
                "label": d.strftime("%-d"),
                "month": d.strftime("%b"),
                "weekend": d.weekday() >= 5,
                "holiday": d in line.holidays,
            }
            for d in days
        ],
        "physicians": [
            {
                "id": p.id, "name": p.name, "specialty": p.specialty,
                "group": group_of(p), "fte": p.fte, "skills": sorted(p.skills),
                "tags": sorted(p.tags), "notes": p.notes,
                "off": [
                    {"date": d.isoformat(), "reason": p.is_unavailable(d)}
                    for d in days if p.is_unavailable(d)
                ],
                "admin": [
                    {"date": d.isoformat(), "label": b.label, "session": b.session,
                     "hard": b.hard}
                    for d in days for b in p.admin_sessions(d, line.holidays)
                ],
            }
            for p in line.physicians
        ],
        "shifts": [
            {
                "id": s.id, "label": s.label, "location": s.location,
                "session": s.session, "tier": s.tier,
                "group": s.fairness_key(),
                "days": [d.isoformat() for d in days
                         if s.recurrence.matches(d, line.holidays)],
            }
            for s in line.shifts
        ],
        "assignments": [
            {"date": a.date.isoformat(), "shift": a.shift_id,
             "physician": a.physician_id, "reason": a.reason}
            for a in result.schedule.assignments
        ],
        "violations": [
            {"severity": v.severity, "date": v.date.isoformat() if v.date else None,
             "kind": v.kind, "message": v.message}
            for v in result.violations
        ],
        "equity": equity,
    }


def write_json(result: SolveResult, path: str | Path) -> None:
    import json
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(schedule_json(result), separators=(",", ":")))
