# MDStaffing

A rule-driven scheduling engine for a **cardiovascular and pulmonary service line** —
physicians split between clinic and hospital, procedural and diagnostic duties that only
some are credentialed for, medical directors with protected admin time, census rules that
cap or floor how many of each kind of physician can be in each place on a given day, and a
fairness rotation that has to hold up over a year rather than a month.

`config/service_line.yaml` is seeded with a 19-physician cardiology division — 3
electrophysiologists, 6 invasive cardiologists covering the cath lab and STEMI call (one
of whom is the sole TAVR operator), and 10 non-invasive cardiologists covering diagnostic
studies, inpatient consults and general cardiology call — plus a placeholder
pulmonary/critical care group to replace with the real one.

Everything is driven by one YAML file. No database, no service to run, no dependencies
beyond PyYAML.

```bash
python -m mdstaffing validate config/service_line.yaml
python -m mdstaffing build    config/service_line.yaml --out out --ledger-out out/ledger.json
open out/schedule.html
```

---

## How the engine schedules

The pipeline mirrors the order a scheduler works in by hand:

1. **Block out who cannot work.** Vacation, leave, approved (`off_hard`) requests and
   protected admin/medical-director time come off the board first.
2. **Lock in what is already decided.** Pre-committed assignments and approved
   "must work this day" requests are placed before anything is optimised.
3. **Fill the scarce, rule-bound work.** Cath lab, TAVR, EP lab, TEE, stress lab,
   echo/nuclear/CT reading lists, bronchoscopy, EBUS.
4. **Fill core coverage.** Inpatient service, CCU, heart failure, ICU, consults, call.
5. **Back-fill.** Satisfy minimum-census rules, honour soft scheduling requests, and place
   whoever is left into the default clinic pool.
6. **Repair.** Targeted one-level swaps to fill anything still short.

Steps 3–4 do not run as rigid phases, because rigid phases produce a classic failure: a
tier-1 duty with six credentialed physicians consumes the only two people who can run the
CCU. Instead the engine walks **day by day and always places the duty with the least slack**
(credentialed-and-available physicians minus positions still to fill). Tier breaks ties, so
in the common case procedural work is still scheduled before residual clinic fill, but a
thin core service is never starved by a comfortable procedural one.

Within the feasible set, the physician is chosen by score (lowest wins):

| Term | What it does |
| --- | --- |
| `fairness` | distance from this physician's expected share of *this duty group* |
| `weekend` | distance from their expected share of weekend/holiday burden |
| `load` | overall clinical days relative to FTE |
| `request` | honours `on_soft` / `off_soft` / `prefer_shift` / `avoid_shift` |
| `soft_rule` | strain the assignment would place on soft census rules |
| `recency` | spaces out repeats of the same duty |
| `continuity` | keeps `block_days` services with one physician for the whole block |
| `weekly_cap`, `consecutive` | backs off before a physician hits their own limits |

Tune them under `settings.weights`. Ties break on a hash of `(seed, physician, duty, date)`,
so the same inputs always produce the same schedule and `--seed` gives you a different but
equally valid one.

### What "fair" means here

Fair is **share of a duty proportional to the share you are expected to carry**, not equal
counts. A physician's expected share of a duty group is

```
fte  ×  their opt-in weight for that duty  ×  fraction of the duty's days they are available
```

So a 0.6 FTE physician gets ~60% of a full-timer's nuclear reading days; a physician who is
not credentialed for the cath lab does not dilute the interventionalists' targets; and
someone on vacation for half the block is not handed a punishing catch-up load afterwards.

The ledger is written to JSON (`--ledger-out`) and read back on the next block
(`--ledger`), so rotations stay fair across the year rather than resetting every month.
`python -m mdstaffing ledger out/ledger.json` prints it.

---

## Modelling your service line

### Physicians

```yaml
- id: inv_okonkwo
  name: David Okonkwo
  specialty: cardiology
  skills: [cath, pci, stemi, structural]  # credentialing — drives eligibility
  tags: [invasive, medical_director]      # group membership
  fte: 1.0
  max_clinical_days_per_week: 5
  max_consecutive_clinical_days: 7
  admin:
    - label: Structural Program Director admin
      session: pm
      hard: false                          # yields when coverage is short
      when: {weekdays: [fri]}
  unavailable:
    - {start: 2026-11-16, end: 2026-11-20, reason: vacation}
  shift_weights:
    GEN_CARD_CALL: 0                       # contractual opt-out; 0.5 = half share
    CARD_CONSULT: 0.5
  excluded_shifts: [EP_ABLATION]
```

`skills` is a free vocabulary — whatever your service line credentials on
(`pci`, `structural`, `ep_ablation`, `tee`, `nuclear_read`, `cardiac_ct`, `ebus`, `icu`, …).

`tags` are group membership rather than credentialing: `invasive`, `ep`, `non_invasive`,
`medical_director`. Use them for duties defined by which group covers them rather than by
a procedure skill — the three separate call pools (STEMI call for the invasive group, EP
call for the electrophysiologists, general cardiology call for the non-invasive group) are
each a duty with `required_tags`. Census rules filter on tags too.

### Duties

```yaml
- id: STRUCTURAL_TAVR
  label: Structural / TAVR
  location: hospital          # hospital | clinic | remote | admin
  session: full               # full | am | pm | background
  tier: 1                     # 1 procedural/scarce, 2 core coverage, 3 residual clinic
  count: 2                    # minimum positions to staff
  max_count: 2                # optional headroom above the minimum
  required_skills: [structural]
  any_skills: [[stress_echo, nuclear_read]]   # one from each inner group
  required_tags: [invasive]                   # group membership, all required
  any_tags: [invasive, ep]                    # or at least one of these
  specialties: [cardiology]
  eligible: [inv_okonkwo, inv_whitfield]      # explicit allow-list
  fairness_group: structural  # duties that share a rotation share a group
  credit: 1.5                 # how heavily one assignment weighs in the rotation
  block_days: 7               # keep one physician on the service for a whole week
  hard: true                  # false = best effort, an empty slot is only a warning
  when:
    nth_weekday_of_month:
      - {week: 2, weekday: wed}
      - {week: 4, weekday: wed}
```

`session: background` is for reading lists and call — work that stacks on top of a clinical
day (zero effort, capped by `settings.max_background_per_day`). `full` consumes the day;
`am`/`pm` consume half, so a physician can do TEE in the morning and something else after.

### Recurrence (`when:`)

| Key | Meaning |
| --- | --- |
| `weekdays: [mon, wed]` | days of the week |
| `nth_weekday_of_month: [{week: 2, weekday: wed}]` | 2nd Wednesday; `week: -1` = last |
| `interval_weeks: 2`, `anchor: 2026-01-05` | every other week from an anchor |
| `dates: [2026-03-04]` | explicit dates (always match) |
| `exclude_dates: [...]` | explicit exceptions |
| `months: [1, 2, 3]` | restrict to months |
| `include_holidays: true` | run on days listed under `holidays:` (default: skip) |

### Census rules

Evaluated per day over the whole service line — the "how many of X can be in Y" layer.

```yaml
- id: hospital_cardiology_cap
  description: no more than 9 cardiologists physically in the hospital in one day
  locations: [hospital]
  sessions: [full, am, pm]      # exclude background reading lists from "presence"
  specialties: [cardiology]
  any_skills: [pci]             # or `skills:` to require all of them
  tags: [medical_director]
  shifts: [CATH_LAB]            # or restrict to specific duties
  physicians: [c_chen]
  max: 9
  min: 1
  distinct: true                # count physicians, not assignments
  severity: hard                # hard = never violated; soft = weighted preference
  weight: 3
  when: {weekdays: [mon, tue, wed, thu, fri]}
```

`severity: hard` maxima are enforced during assignment and can never be exceeded — if that
makes a duty unfillable, the gap is reported instead. `min` rules drive the back-fill stage.

### Requests

```yaml
requests:
  - {physician: ni_hart,    kind: off_hard,     start: 2026-11-25, end: 2026-11-27}
  - {physician: inv_yun,    kind: off_soft,     start: 2026-11-16, end: 2026-11-18, weight: 2}
  - {physician: ni_kwon,    kind: prefer_shift, date: 2026-11-10, shift: TEE_SERVICE, weight: 3}
  - {physician: ni_adeyemi, kind: avoid_shift,  start: 2026-11-01, end: 2026-11-30, shift: GEN_CARD_CALL, weight: 3}
  - {physician: ep_vasquez, kind: on_soft,      date: 2026-11-03, shift: LEAD_EXTRACTION, weight: 3}
  - {physician: ep_vasquez, kind: on_hard,      date: 2026-11-03, shift: LEAD_EXTRACTION}
```

`off_hard` and `on_hard` are commitments the solver may never break. The rest are weighted
preferences competing against fairness — raise `weight` to make one harder to overrule.

---

## Commands

| Command | Purpose |
| --- | --- |
| `validate CONFIG` | check the config without scheduling |
| `build CONFIG --out DIR` | generate the schedule and all reports |
| `build … --ledger L.json --ledger-out L.json` | carry fairness across blocks |
| `build … --strict` | exit non-zero if any hard rule is violated (for CI) |
| `build … --start / --end` | override the period without editing the config |
| `explain CONFIG --date D --shift S` | who can cover this duty, and why the others can't |
| `ledger L.json` | print accumulated credit per physician per duty group |

`explain` is the tool for arguing with the schedule:

```
$ python -m mdstaffing explain config/service_line.yaml --date 2026-11-25 --shift HF_SERVICE
Heart failure service on Wed 2026-11-25 — assigned: (nobody)

Physician                Eligible   Credit  Status
Benjamin Hart            yes           9.0  approved time off: approved holiday leave
Fatima Nasser            yes           9.0  would make 8 consecutive clinical days (cap 7)
Thomas Reiner            no            0.0  lacks hf
```

It names the real blocker — a credentialing gap, protected time, a census rule, a personal
cap — rather than just reporting that the slot was hard to fill.

## Outputs (`--out`)

| File | Contents |
| --- | --- |
| `schedule.html` | one page: coverage grid, per-physician grid, equity, rule check |
| `coverage.csv` | duties × days — the service-line chief's view |
| `by_physician.csv` | physicians × days, showing vacation, admin and `available` |
| `equity.csv` | assigned vs. expected share per duty group, plus cumulative credit |
| `rule_check.csv` | every hard violation and soft warning, with the rule that fired |
| `schedule.ics` | calendar feed (all-day events) for import |
| `ledger.json` | fairness carry-in for the next block (`--ledger-out`) |

## Reading the rule check

Every schedule is re-validated from scratch after solving, independently of the code that
built it, so the report is a real audit rather than a restatement of the solver's intent.

* **hard** — the schedule is not publishable. Either coverage is genuinely impossible with
  the people available, or a rule is stricter than the roster can support. Use `explain` on
  the date and duty; the answer is usually "this cap is the binding constraint" or "these
  two physicians cannot cover three simultaneous full-day duties".
* **soft** — a preference the solver could not fully satisfy (a clinic-access floor missed
  on a heavy procedure day, a soft time-off request overruled). These are the trade-offs to
  review, not errors.

Unused headroom between a duty's `count` and `max_count` is spare capacity and is not
reported as a shortfall.

## Limitations

* The engine is a deterministic greedy solver with a bounded repair pass, not an exact
  optimiser. It reliably produces feasible, fair schedules and reports honestly when it
  cannot, but it does not prove optimality. If a block is tightly constrained, try a few
  `--seed` values and compare the rule checks.
* The repair pass does one-level swaps only; it will not unwind a long chain.
* Scheduling is by day and session (`full`/`am`/`pm`/`background`). Hour-level shift
  boundaries, overnight hand-offs and post-call rest days are not modelled — if you need a
  post-call day off, model call as a duty with `block_days` or add an `off_hard` request.
* There is no ACGME-style resident duty-hour engine here; the caps are the per-physician
  weekly and consecutive-day limits.

## Development

```bash
pip install pyyaml pytest
python -m pytest tests -q
```

`tests/test_scheduler.py` covers recurrence maths, config validation, every hard constraint
(vacation, credentialing, tag-restricted call pools, admin time, census caps,
double-booking, weekly caps, locked assignments), the fairness properties (equal split,
FTE proportionality, vacation handling, carry-in ledger, opt-outs), block continuity,
single-operator gaps, request handling, determinism, and an end-to-end run of the real
service-line config.
