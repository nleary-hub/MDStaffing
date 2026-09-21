"""YAML -> :class:`ServiceLine` loading, with validation of the obvious foot-guns."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import yaml

from .models import (
    AdminBlock,
    Assignment,
    ConfigError,
    DayRule,
    Physician,
    Recurrence,
    Request,
    ServiceLine,
    ShiftDef,
    Unavailability,
    as_list,
    parse_date,
)

REQUEST_KINDS = {"off_hard", "off_soft", "on_soft", "on_hard", "avoid_shift", "prefer_shift"}


def _date_range(cfg: Any, label: str) -> tuple[dt.date, dt.date]:
    if isinstance(cfg, dict):
        if "date" in cfg:
            d = parse_date(cfg["date"])
            return d, d
        if "start" in cfg:
            start = parse_date(cfg["start"])
            return start, parse_date(cfg.get("end", cfg["start"]))
    if isinstance(cfg, (str, dt.date, dt.datetime)):
        d = parse_date(cfg)
        return d, d
    raise ConfigError(f"{label}: expected a date or start/end mapping, got {cfg!r}")


def _physician(cfg: dict) -> Physician:
    admin = []
    for block in as_list(cfg.get("admin")):
        if isinstance(block, str):
            block = {"label": block}
        admin.append(
            AdminBlock(
                label=block.get("label", "admin"),
                session=block.get("session", "full"),
                recurrence=Recurrence.from_config(block.get("recurrence", block.get("when"))),
                hard=bool(block.get("hard", True)),
            )
        )
    unavailable = []
    for block in as_list(cfg.get("unavailable", cfg.get("vacation"))):
        reason = block.get("reason", "vacation") if isinstance(block, dict) else "vacation"
        start, end = _date_range(block, f"{cfg.get('id')} unavailable")
        if end < start:
            raise ConfigError(f"{cfg.get('id')}: unavailable block ends before it starts")
        unavailable.append(Unavailability(start, end, reason))
    return Physician(
        id=str(cfg["id"]),
        name=cfg.get("name", str(cfg["id"])),
        specialty=cfg.get("specialty", "cardiology"),
        skills=set(as_list(cfg.get("skills"))),
        tags=set(as_list(cfg.get("tags"))),
        fte=float(cfg.get("fte", 1.0)),
        max_clinical_days_per_week=(
            float(cfg["max_clinical_days_per_week"])
            if cfg.get("max_clinical_days_per_week") is not None
            else None
        ),
        max_consecutive_clinical_days=int(cfg.get("max_consecutive_clinical_days", 7)),
        admin=admin,
        unavailable=unavailable,
        excluded_shifts=set(as_list(cfg.get("excluded_shifts"))),
        shift_weights={str(k): float(v) for k, v in (cfg.get("shift_weights") or {}).items()},
        notes=cfg.get("notes", ""),
    )


def _shift(cfg: dict) -> ShiftDef:
    any_skills = [as_list(group) for group in as_list(cfg.get("any_skills"))]
    return ShiftDef(
        id=str(cfg["id"]),
        label=cfg.get("label", str(cfg["id"])),
        location=cfg.get("location", "hospital"),
        session=cfg.get("session", "full"),
        tier=int(cfg.get("tier", 2)),
        required_skills=as_list(cfg.get("required_skills")),
        any_skills=any_skills,
        specialties=as_list(cfg.get("specialties")),
        eligible=as_list(cfg.get("eligible")),
        min_count=int(cfg.get("count", cfg.get("min_count", 1))),
        max_count=(int(cfg["max_count"]) if cfg.get("max_count") is not None else None),
        recurrence=Recurrence.from_config(cfg.get("recurrence", cfg.get("when"))),
        fairness_group=cfg.get("fairness_group", ""),
        credit=float(cfg.get("credit", 1.0)),
        effort=float(cfg.get("effort", 0.0 if cfg.get("session") == "background" else
                             (0.5 if cfg.get("session") in ("am", "pm") else 1.0))),
        block_days=int(cfg.get("block_days", 1)),
        tags=set(as_list(cfg.get("tags"))),
        hard=bool(cfg.get("hard", True)),
    )


def _rule(cfg: dict) -> DayRule:
    return DayRule(
        id=str(cfg["id"]),
        description=cfg.get("description", ""),
        locations=as_list(cfg.get("locations", cfg.get("location"))),
        sessions=as_list(cfg.get("sessions", cfg.get("session"))),
        shift_ids=as_list(cfg.get("shifts", cfg.get("shift_ids"))),
        skills=as_list(cfg.get("skills")),
        any_skills=as_list(cfg.get("any_skills")),
        specialties=as_list(cfg.get("specialties", cfg.get("specialty"))),
        tags=as_list(cfg.get("tags")),
        physicians=as_list(cfg.get("physicians")),
        min_count=(int(cfg["min"]) if cfg.get("min") is not None else None),
        max_count=(int(cfg["max"]) if cfg.get("max") is not None else None),
        distinct=bool(cfg.get("distinct", True)),
        recurrence=Recurrence.from_config(cfg.get("recurrence", cfg.get("when"))),
        severity=cfg.get("severity", "hard"),
        weight=float(cfg.get("weight", 1.0)),
    )


def _request(cfg: dict) -> Request:
    kind = cfg.get("kind", "off_soft")
    if kind not in REQUEST_KINDS:
        raise ConfigError(f"unknown request kind {kind!r}; expected one of {sorted(REQUEST_KINDS)}")
    start, end = _date_range(cfg, "request")
    return Request(
        physician_id=str(cfg["physician"]),
        kind=kind,
        start=start,
        end=end,
        shift_id=cfg.get("shift"),
        weight=float(cfg.get("weight", 1.0)),
        note=cfg.get("note", ""),
    )


def load_service_line(path: str | Path) -> ServiceLine:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level of the config must be a mapping")

    period = raw.get("period") or {}
    if not period.get("start") or not period.get("end"):
        raise ConfigError("config needs period.start and period.end")
    start, end = parse_date(period["start"]), parse_date(period["end"])
    if end < start:
        raise ConfigError("period.end is before period.start")

    physicians = [_physician(p) for p in as_list(raw.get("physicians"))]
    shifts = [_shift(s) for s in as_list(raw.get("shifts"))]
    rules = [_rule(r) for r in as_list(raw.get("rules"))]
    requests = [_request(r) for r in as_list(raw.get("requests"))]
    holidays = {parse_date(d) for d in as_list(raw.get("holidays"))}
    locked = [
        Assignment(
            date=parse_date(a["date"]),
            shift_id=str(a["shift"]),
            physician_id=str(a["physician"]),
            index=int(a.get("index", 0)),
            reason="locked",
        )
        for a in as_list(raw.get("locked"))
    ]

    line = ServiceLine(
        name=raw.get("name", "service line"),
        start=start,
        end=end,
        physicians=physicians,
        shifts=shifts,
        rules=rules,
        requests=requests,
        holidays=holidays,
        locked=locked,
        settings=raw.get("settings") or {},
    )
    validate(line)
    return line


def validate(line: ServiceLine) -> None:
    """Fail loudly on references that can never resolve."""
    errors: list[str] = []
    if not line.physicians:
        errors.append("no physicians defined")
    if not line.shifts:
        errors.append("no shifts defined")

    seen: set[str] = set()
    for doc in line.physicians:
        if doc.id in seen:
            errors.append(f"duplicate physician id {doc.id!r}")
        seen.add(doc.id)
    seen.clear()
    for shift in line.shifts:
        if shift.id in seen:
            errors.append(f"duplicate shift id {shift.id!r}")
        seen.add(shift.id)
        if shift.session not in ("full", "am", "pm", "background"):
            errors.append(f"{shift.id}: unknown session {shift.session!r}")
        for pid in shift.eligible:
            if pid not in line.physician_by_id:
                errors.append(f"{shift.id}: eligible lists unknown physician {pid!r}")

    all_skills = set().union(*[p.skills for p in line.physicians]) if line.physicians else set()
    for shift in line.shifts:
        unknown = set(shift.required_skills) - all_skills
        if unknown:
            errors.append(f"{shift.id}: no physician holds required skill(s) {sorted(unknown)}")
    for rule in line.rules:
        for sid in rule.shift_ids:
            if sid not in line.shift_by_id:
                errors.append(f"rule {rule.id}: unknown shift {sid!r}")
        if rule.min_count is None and rule.max_count is None:
            errors.append(f"rule {rule.id}: needs a min and/or max")
        if rule.severity not in ("hard", "soft"):
            errors.append(f"rule {rule.id}: severity must be hard or soft")
    for req in line.requests:
        if req.physician_id not in line.physician_by_id:
            errors.append(f"request references unknown physician {req.physician_id!r}")
        if req.shift_id and req.shift_id not in line.shift_by_id:
            errors.append(f"request references unknown shift {req.shift_id!r}")
    for a in line.locked:
        if a.physician_id not in line.physician_by_id:
            errors.append(f"locked assignment references unknown physician {a.physician_id!r}")
        if a.shift_id not in line.shift_by_id:
            errors.append(f"locked assignment references unknown shift {a.shift_id!r}")

    if errors:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))
