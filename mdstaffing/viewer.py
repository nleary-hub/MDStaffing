"""Self-contained interactive schedule board.

Emits one HTML file with the solved schedule embedded as JSON, so a service-line
chief can scan coverage, check a physician's month, read the equity picture and
work the rule findings without running anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .report import schedule_json
from .solver import SolveResult

GROUP_LABELS = {
    "invasive": "Invasive",
    "ep": "Electrophysiology",
    "non_invasive": "Non-invasive",
    "pulm": "Pulmonary",
}
TIER_LABELS = {1: "Procedural & diagnostic", 2: "Inpatient coverage & call", 3: "Clinic"}


def _short_codes(shifts: list[dict]) -> None:
    """Give every duty a compact, unique cell code for the dense physician grid."""
    used: set[str] = set()
    for shift in shifts:
        text = re.sub(r"\(.*?\)", " ", shift["label"])
        words = [w for w in re.split(r"[^A-Za-z]+", text) if len(w) > 1]
        code = (words[0][:4] if len(words) == 1 else "".join(w[0] for w in words[:4])).upper()
        base, n = code or "X", 2
        while code in used:
            code = f"{base}{n}"
            n += 1
        used.add(code)
        shift["short"] = code


TEMPLATE = """<title>Service Line Board</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,500;6..72,600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@400;600&display=swap">
<style>
:root{
  --paper:#EEF2F3; --surface:#FFFFFF; --sunken:#E3EAEC;
  --ink:#13212A; --ink-2:#48606B; --ink-3:#7A8F98;
  --line:#CEDADE; --line-2:#E2EAEC;
  --accent:#0F5E70; --accent-ink:#FFFFFF; --accent-soft:#DCEDF0;
  --g-inv:#D55E00; --g-ep:#007A5A; --g-non:#0072B2; --g-pulm:#6B7F87;
  --over:#D55E00; --under:#0072B2; --zero:#9FB0B6;
  --ok:#1C7048; --warn:#8A5A00; --crit:#A32A22;
  --ok-bg:#DFF0E7; --warn-bg:#F7EBD2; --crit-bg:#F7DEDB;
  --off:#E7ECEE; --shadow:0 1px 2px rgba(19,33,42,.08),0 8px 24px -16px rgba(19,33,42,.35);
  --r:6px;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#0C1519; --surface:#152128; --sunken:#0F1A1F;
  --ink:#E2ECEF; --ink-2:#9DB2BA; --ink-3:#6E868F;
  --line:#2A3B43; --line-2:#1E2C33;
  --accent:#4FB3C6; --accent-ink:#06212A; --accent-soft:#14333B;
  --g-inv:#D4762E; --g-ep:#12A87A; --g-non:#3E97D2; --g-pulm:#7E9098;
  --over:#D4762E; --under:#3E97D2; --zero:#5B7079;
  --ok:#4BB58A; --warn:#D3A02F; --crit:#E8877B;
  --ok-bg:#12312A; --warn-bg:#33270F; --crit-bg:#3A1F1D;
  --off:#1B2930; --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -16px rgba(0,0,0,.8);
}}
:root[data-theme="dark"]{
  --paper:#0C1519; --surface:#152128; --sunken:#0F1A1F;
  --ink:#E2ECEF; --ink-2:#9DB2BA; --ink-3:#6E868F;
  --line:#2A3B43; --line-2:#1E2C33;
  --accent:#4FB3C6; --accent-ink:#06212A; --accent-soft:#14333B;
  --g-inv:#D4762E; --g-ep:#12A87A; --g-non:#3E97D2; --g-pulm:#7E9098;
  --over:#D4762E; --under:#3E97D2; --zero:#5B7079;
  --ok:#4BB58A; --warn:#D3A02F; --crit:#E8877B;
  --ok-bg:#12312A; --warn-bg:#33270F; --crit-bg:#3A1F1D;
  --off:#1B2930; --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -16px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1500px;margin:0 auto;padding:0 16px;padding-block:20px 56px}
h1,h2,h3{margin:0;text-wrap:balance}
button{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}

/* ---- masthead ---- */
.mast{display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px 24px;
  padding-bottom:16px;border-bottom:2px solid var(--ink)}
.mast h1{font-family:Newsreader,Georgia,serif;font-weight:600;font-size:30px;
  letter-spacing:-.01em;line-height:1.1}
.mast .sub{color:var(--ink-2);font-size:13px;margin-top:3px;
  font-variant-numeric:tabular-nums}
.statwrap{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  padding:7px 11px;min-width:76px}
.stat b{display:block;font-size:19px;line-height:1.15;font-variant-numeric:tabular-nums}
.stat span{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--ink-3);margin-top:1px}
.stat.crit b{color:var(--crit)} .stat.ok b{color:var(--ok)} .stat.warn b{color:var(--warn)}

/* ---- controls ---- */
.bar{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;
  padding:14px 0;position:sticky;top:env(safe-area-inset-top,0px);z-index:20;
  background:var(--paper);border-bottom:1px solid var(--line)}
.tabs{display:flex;gap:2px;background:var(--sunken);padding:3px;border-radius:8px}
.tabs button{border:0;background:none;padding:6px 13px;border-radius:6px;cursor:pointer;
  font-size:13.5px;font-weight:500;color:var(--ink-2)}
.tabs button[aria-selected="true"]{background:var(--surface);color:var(--ink);
  box-shadow:0 1px 2px rgba(0,0,0,.12)}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);
  background:var(--surface);border-radius:999px;padding:4px 11px 4px 8px;font-size:12.5px;
  cursor:pointer;color:var(--ink-2)}
.chip[aria-pressed="true"]{border-color:var(--ink-2);color:var(--ink);font-weight:500}
.chip[aria-pressed="false"]{opacity:.5}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.g-invasive{background:var(--g-inv)} .g-ep{background:var(--g-ep)}
.g-non_invasive{background:var(--g-non)} .g-pulm{background:var(--g-pulm)}
input[type=search]{border:1px solid var(--line);background:var(--surface);color:var(--ink);
  border-radius:var(--r);padding:6px 10px;font-size:13.5px;min-width:0;width:170px}
.ghost{border:1px solid var(--line);background:var(--surface);border-radius:var(--r);
  padding:6px 11px;font-size:12.5px;cursor:pointer;color:var(--ink-2)}

/* ---- board ---- */
.board{margin-top:18px;background:var(--surface);border:1px solid var(--line);
  border-radius:10px;box-shadow:var(--shadow);overflow:hidden}
.scroll{overflow-x:auto;overflow-y:visible}
table{border-collapse:separate;border-spacing:0;width:100%;
  font-family:"IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;font-size:12.5px}
th,td{border-bottom:1px solid var(--line-2);padding:0;text-align:left;vertical-align:middle}
thead th{position:sticky;top:0;z-index:6;background:var(--surface);
  border-bottom:1px solid var(--line);padding:7px 4px;font-weight:600;text-align:center;
  font-size:11px;color:var(--ink-2);white-space:nowrap}
thead th.corner{z-index:8;text-align:left;padding-left:12px}
th.rowhead,td.rowhead{position:sticky;left:0;z-index:5;background:var(--surface);
  border-right:1px solid var(--line);min-width:186px;max-width:186px;
  padding:6px 10px;font-family:"IBM Plex Sans",sans-serif;font-weight:500;font-size:12.5px}
td.rowhead .meta{display:block;color:var(--ink-3);font-size:10.5px;font-weight:400;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.daycol{min-width:38px}
.dayhead{cursor:pointer;background:none;border:0;padding:2px 3px;width:100%;
  border-radius:5px;line-height:1.15;font:inherit;color:inherit}
.dayhead:hover{background:var(--accent-soft)}
.dayhead .wd{display:block;font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--ink-3)}
.dayhead .dd{display:block;font-size:13px;font-weight:600;font-variant-numeric:tabular-nums}
th.we,td.we{background:var(--sunken)}
th.hol .dd{color:var(--crit)}
td.cell{padding:3px 4px;text-align:center;line-height:1.25}
.name{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.code{display:inline-block;padding:1px 4px;border-radius:4px;font-weight:600;
  font-size:11px;letter-spacing:.02em}
.loc-hospital{background:var(--accent-soft);color:var(--accent)}
.loc-clinic{background:var(--ok-bg);color:var(--ok)}
.loc-remote{background:var(--sunken);color:var(--ink-2)}
td.offday{background:var(--off);color:var(--ink-3);font-size:10px;letter-spacing:.06em}
tr.grp th{background:var(--sunken);font-family:"IBM Plex Sans",sans-serif;
  font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--ink-2);
  padding:5px 12px;font-weight:600;position:sticky;left:0;z-index:5}
tbody tr:hover td:not(.rowhead){background:var(--accent-soft)}
tbody tr:hover td.rowhead{background:var(--accent-soft)}

/* ---- code key ---- */
.key{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;padding:11px 14px;
  border-bottom:1px solid var(--line);background:var(--sunken);font-size:11.5px}
.keyhead{font-weight:600;text-transform:uppercase;letter-spacing:.08em;font-size:10.5px;
  color:var(--ink-3);margin-right:2px}
.keyitem{display:inline-flex;align-items:center;gap:5px;color:var(--ink-2);white-space:nowrap}

/* ---- equity ---- */
.eq{padding:18px 16px}
.eqgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:22px}
.eqgroup h3{font-family:"IBM Plex Sans",sans-serif;font-size:12px;text-transform:uppercase;
  letter-spacing:.08em;color:var(--ink-2);margin-bottom:2px}
.eqgroup .cap{font-size:11.5px;color:var(--ink-3);margin-bottom:10px}
.eqrow{display:grid;grid-template-columns:104px 1fr 54px;gap:8px;align-items:center;
  margin-bottom:5px;font-size:12px}
.eqrow .who{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink-2)}
.track{position:relative;height:15px;background:var(--sunken);border-radius:3px}
.track .axis{position:absolute;top:-1px;bottom:-1px;left:50%;width:1px;background:var(--zero)}
.track .bar{position:absolute;top:2px;bottom:2px;border-radius:2px}
.track .bar.pos{background:var(--over);border-radius:2px 4px 4px 2px}
.track .bar.neg{background:var(--under);border-radius:4px 2px 2px 4px}
.eqrow .val{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink-2)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:11.5px;color:var(--ink-2);
  margin-bottom:14px;align-items:center}
.legend i{display:inline-block;width:14px;height:9px;border-radius:2px;margin-right:5px;
  vertical-align:middle}

/* ---- findings ---- */
.find{padding:8px 0}
.frow{display:flex;gap:12px;align-items:baseline;padding:10px 16px;
  border-bottom:1px solid var(--line-2)}
.frow:last-child{border-bottom:0}
.sev{flex:none;font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;
  padding:2px 7px;border-radius:4px}
.sev.hard{background:var(--crit-bg);color:var(--crit)}
.sev.soft{background:var(--warn-bg);color:var(--warn)}
.fdate{flex:none;color:var(--ink-3);font-size:12px;font-variant-numeric:tabular-nums;
  min-width:96px}
.clear{padding:34px 16px;text-align:center;color:var(--ink-2)}
.clear b{display:block;font-family:Newsreader,Georgia,serif;font-size:19px;color:var(--ok);
  margin-bottom:4px}

/* ---- day panel ---- */
.scrim{position:fixed;inset:0;background:rgba(10,20,25,.42);z-index:60;border:0;padding:0}
.panel{position:fixed;top:0;right:0;bottom:0;width:min(420px,100%);z-index:61;
  background:var(--surface);border-left:1px solid var(--line);overflow-y:auto;
  padding:20px 20px calc(28px + env(safe-area-inset-bottom,0px));
  padding-top:calc(20px + env(safe-area-inset-top,0px))}
.panel h2{font-family:Newsreader,Georgia,serif;font-size:22px;font-weight:600}
.panel .psub{color:var(--ink-2);font-size:12.5px;margin-top:2px}
.panel .close{position:absolute;top:calc(16px + env(safe-area-inset-top,0px));right:16px;
  border:1px solid var(--line);background:var(--surface);border-radius:var(--r);
  width:30px;height:30px;cursor:pointer;line-height:1}
.pgroup{margin-top:18px}
.pgroup h4{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--ink-3);
  margin:0 0 7px;font-weight:600}
.pitem{display:flex;gap:10px;justify-content:space-between;padding:6px 0;
  border-bottom:1px solid var(--line-2);font-size:13px;gap:14px}
.pitem .d{color:var(--ink-2);flex:1;min-width:0}
.pitem .p{text-align:right;font-weight:500}
.free{display:flex;flex-wrap:wrap;gap:5px}
.pill{border:1px solid var(--line);border-radius:999px;padding:2px 9px;font-size:11.5px;
  color:var(--ink-2)}
.pill.offp{background:var(--off);border-color:transparent}
@media (prefers-reduced-motion:no-preference){.panel{animation:slide .18s ease-out}}
@keyframes slide{from{transform:translateX(14px);opacity:.4}to{transform:none;opacity:1}}
@media (max-width:560px){
  .mast h1{font-size:24px} .statwrap{margin-left:0}
  th.rowhead,td.rowhead{min-width:132px;max-width:132px}
  .eqrow{grid-template-columns:86px 1fr 46px}
}
</style>

<div class="wrap">
  <header class="mast">
    <div>
      <h1 id="ttl"></h1>
      <div class="sub" id="period"></div>
    </div>
    <div class="statwrap" id="stats"></div>
  </header>

  <div class="bar">
    <div class="tabs" role="tablist" id="tabs">
      <button role="tab" data-v="coverage" aria-selected="true">Coverage</button>
      <button role="tab" data-v="people" aria-selected="false">Physicians</button>
      <button role="tab" data-v="equity" aria-selected="false">Equity</button>
      <button role="tab" data-v="findings" aria-selected="false">Rule check</button>
    </div>
    <div class="chips" id="chips"></div>
    <input type="search" id="q" placeholder="Find a physician" aria-label="Find a physician">
    <button class="ghost" id="theme" type="button">Dark</button>
  </div>

  <div class="board"><div class="scroll" id="view"></div></div>
  <p style="color:var(--ink-3);font-size:11.5px;margin-top:12px">
    Generated by MDStaffing. Click any date to open that day&rsquo;s full roster.
    Seed roster &mdash; names are illustrative.</p>
</div>

<script>
const DATA = __DATA__;
const view_ = document.getElementById("view"), q_ = document.getElementById("q");
const GL = __GROUPS__, TL = __TIERS__;
const A = {}, D = {}, OFF = {}, ADM = {};
DATA.assignments.forEach(a=>{
  (A[a.date+"|"+a.shift] ||= []).push(a.physician);
  (D[a.date+"|"+a.physician] ||= []).push(a.shift);
});
const DOC = Object.fromEntries(DATA.physicians.map(p=>[p.id,p]));
const SH  = Object.fromEntries(DATA.shifts.map(s=>[s.id,s]));
DATA.physicians.forEach(p=>{
  p.off.forEach(o=>OFF[o.date+"|"+p.id]=o.reason);
  p.admin.forEach(o=>ADM[o.date+"|"+p.id]=o);
});
const surname = n => n.split(" ").slice(-1)[0];
const GROUPS = ["invasive","ep","non_invasive","pulm"].filter(g=>DATA.physicians.some(p=>p.group===g));
let view="coverage", active=new Set(GROUPS), q="";
try{const s=JSON.parse(localStorage.getItem("mdstaff")||"{}");
  if(s.view) view=s.view; if(s.theme) document.documentElement.dataset.theme=s.theme;}catch(e){}
const save=()=>{try{localStorage.setItem("mdstaff",JSON.stringify(
  {view,theme:document.documentElement.dataset.theme||""}));}catch(e){}};

/* ---- header ---- */
const hard = DATA.violations.filter(v=>v.severity==="hard");
const soft = DATA.violations.filter(v=>v.severity==="soft");
ttl.textContent = DATA.name;
const fmt = d => new Date(d+"T12:00").toLocaleDateString(undefined,{month:"long",day:"numeric",year:"numeric"});
period.textContent = fmt(DATA.start)+" \\u2013 "+fmt(DATA.end);
stats.innerHTML = [
  ["physicians", DATA.physicians.length, ""],
  ["assignments", DATA.assignments.length, ""],
  ["hard violations", hard.length, hard.length?"crit":"ok"],
  ["soft warnings", soft.length, soft.length?"warn":"ok"],
].map(([l,v,c])=>`<div class="stat ${c}"><b>${v}</b><span>${l}</span></div>`).join("");

chips.innerHTML = GROUPS.map(g=>
  `<button class="chip" data-g="${g}" aria-pressed="true"><i class="dot g-${g}"></i>${GL[g]}</button>`).join("");
chips.onclick = e => { const b=e.target.closest(".chip"); if(!b) return;
  const g=b.dataset.g; active.has(g)?active.delete(g):active.add(g);
  b.setAttribute("aria-pressed", active.has(g)); render(); };
tabs.onclick = e => { const b=e.target.closest("button"); if(!b) return;
  view=b.dataset.v; [...tabs.children].forEach(x=>x.setAttribute("aria-selected",x===b)); save(); render(); };
q_.oninput = e => { q=e.target.value.toLowerCase(); render(); };
theme.onclick = () => { const r=document.documentElement;
  const dark = r.dataset.theme ? r.dataset.theme==="dark"
    : matchMedia("(prefers-color-scheme:dark)").matches;
  r.dataset.theme = dark?"light":"dark"; theme.textContent = dark?"Dark":"Light"; save(); };

/* ---- shared grid pieces ---- */
const dayTh = () => DATA.days.map(d=>
  `<th class="daycol ${d.weekend?"we":""} ${d.holiday?"hol":""}">
     <button class="dayhead" data-d="${d.date}"><span class="wd">${d.weekday[0]}</span>
     <span class="dd">${d.label}</span></button></th>`).join("");

function coverage(){
  let h = `<table><thead><tr><th class="corner rowhead">Duty</th>${dayTh()}</tr></thead><tbody>`;
  for (const tier of [1,2,3]){
    const rows = DATA.shifts.filter(s=>s.tier===tier && s.days.length);
    if(!rows.length) continue;
    h += `<tr class="grp"><th colspan="${DATA.days.length+1}">${TL[tier]}</th></tr>`;
    for(const s of rows){
      h += `<tr><td class="rowhead">${s.label}
        <span class="meta">${s.location} &middot; ${s.session}</span></td>`;
      for(const d of DATA.days){
        const on = A[d.date+"|"+s.id]||[];
        const txt = on.map(p=>surname(DOC[p].name)).join(", ");
        h += `<td class="cell ${d.weekend?"we":""}" title="${txt?s.label+": "+on.map(p=>DOC[p].name).join(", "):""}">
              <span class="name">${txt}</span></td>`;
      }
      h += `</tr>`;
    }
  }
  return h+`</tbody></table>`;
}

function people(){
  const seen = DATA.shifts.filter(s=>s.days.length);
  let h = `<div class="key"><span class="keyhead">Duty codes</span>` +
    seen.map(s=>`<span class="keyitem"><span class="code loc-${s.location}">${s.short}</span>${s.label}</span>`).join("") +
    `<span class="keyitem"><span class="code loc-remote">ADM</span>protected admin time</span>` +
    `<span class="keyitem"><span class="code" style="background:var(--off);color:var(--ink-3)">VAC</span>away</span></div>` +
    `<div class="scroll"><table><thead><tr><th class="corner rowhead">Physician</th>${dayTh()}</tr></thead><tbody>`;
  for(const g of GROUPS){
    if(!active.has(g)) continue;
    const rows = DATA.physicians.filter(p=>p.group===g && p.name.toLowerCase().includes(q));
    if(!rows.length) continue;
    h += `<tr class="grp"><th colspan="${DATA.days.length+1}">${GL[g]} &middot; ${rows.length}</th></tr>`;
    for(const p of rows){
      h += `<tr><td class="rowhead">${p.name}
        <span class="meta">${p.fte<1?p.fte.toFixed(1)+" FTE \\u00b7 ":""}${p.skills.slice(0,3).join(", ")}</span></td>`;
      for(const d of DATA.days){
        const on = D[d.date+"|"+p.id]||[], off = OFF[d.date+"|"+p.id], ad = ADM[d.date+"|"+p.id];
        if(on.length){
          const codes = on.map(s=>`<span class="code loc-${SH[s].location}">${SH[s].short}</span>`).join(" ");
          h += `<td class="cell ${d.weekend?"we":""}" title="${on.map(s=>SH[s].label).join(" + ")}">${codes}</td>`;
        } else if(off){
          h += `<td class="cell offday" title="${off}">${off.slice(0,3).toUpperCase()}</td>`;
        } else if(ad){
          h += `<td class="cell ${d.weekend?"we":""}" title="${ad.label}">
                <span class="code loc-remote">ADM</span></td>`;
        } else h += `<td class="cell ${d.weekend?"we":""}"></td>`;
      }
      h += `</tr>`;
    }
  }
  return h+`</tbody></table></div>`;
}

function equity(){
  const groups = {};
  DATA.equity.forEach(r=>{ if(r.group.startsWith("_")&&r.group!=="_weekend") return;
    (groups[r.group] ||= []).push(r); });
  const nice = g => g==="_weekend" ? "Weekend & holiday burden"
    : g.replace(/_/g," ").replace(/\\b\\w/g,c=>c.toUpperCase());
  let h = `<div class="eq"><div class="legend">
     <span><i style="background:var(--under)"></i>Below expected share</span>
     <span><i style="background:var(--over)"></i>Above expected share</span>
     <span>Expected = FTE &times; duty opt-in &times; availability during the block.</span>
   </div><div class="eqgrid">`;
  for(const g of Object.keys(groups).sort()){
    const rows = groups[g].filter(r=>{
      const p = DATA.physicians.find(x=>x.name===r.physician);
      return p && active.has(p.group) && r.physician.toLowerCase().includes(q);
    });
    if(!rows.length) continue;
    const max = Math.max(1, ...rows.map(r=>Math.abs(r.delta)));
    h += `<div class="eqgroup"><h3>${nice(g)}</h3>
      <p class="cap">${rows.length} physicians &middot; widest gap ${max.toFixed(1)} credits</p>`;
    for(const r of rows.sort((a,b)=>b.delta-a.delta)){
      const w = Math.abs(r.delta)/max*50, pos = r.delta>=0;
      h += `<div class="eqrow" title="${r.physician}: ${r.assigned} assigned vs ${r.expected.toFixed(1)} expected">
        <span class="who">${r.physician}</span>
        <span class="track"><span class="axis"></span>
          <span class="bar ${pos?"pos":"neg"}" style="${pos?"left:50%":"right:50%"};width:${w}%"></span></span>
        <span class="val">${r.delta>0?"+":""}${r.delta.toFixed(1)}</span></div>`;
    }
    h += `</div>`;
  }
  return h+`</div></div>`;
}

function findings(){
  if(!DATA.violations.length)
    return `<div class="clear"><b>No rule violations</b>Every duty is staffed and every census rule holds.</div>`;
  const order = {hard:0,soft:1};
  const rows = [...DATA.violations].sort((a,b)=>order[a.severity]-order[b.severity]
    || (a.date||"").localeCompare(b.date||""));
  return `<div class="find">`+rows.map(v=>`<div class="frow">
      <span class="sev ${v.severity}">${v.severity}</span>
      <span class="fdate">${v.date||""}</span>
      <span>${v.message}</span></div>`).join("")+`</div>`;
}

function render(){
  chips.parentElement.querySelector("#q").hidden = (view==="findings");
  chips.hidden = (view==="coverage"||view==="findings");
  view_.innerHTML = view==="coverage"?coverage():view==="people"?people()
    :view==="equity"?equity():findings();
  view_.classList.toggle("scroll", view==="coverage");
}

/* ---- day panel ---- */
document.addEventListener("click", e=>{
  const b = e.target.closest(".dayhead"); if(b) openDay(b.dataset.d);
});
function openDay(date){
  closeDay();
  const d = DATA.days.find(x=>x.date===date);
  const duties = DATA.shifts.filter(s=>s.days.includes(date));
  const working = new Set(); duties.forEach(s=>(A[date+"|"+s.id]||[]).forEach(p=>working.add(p)));
  const offs = DATA.physicians.filter(p=>OFF[date+"|"+p.id]);
  const free = DATA.physicians.filter(p=>!working.has(p.id) && !OFF[date+"|"+p.id]);
  const list = duties.map(s=>{
    const on = (A[date+"|"+s.id]||[]).map(p=>DOC[p].name).join(", ");
    return `<div class="pitem"><span class="d">${s.label}</span>
      <span class="p" style="${on?"":"color:var(--crit)"}">${on||"unfilled"}</span></div>`;
  }).join("");
  const vs = DATA.violations.filter(v=>v.date===date);
  const s = document.createElement("div"); s.className="scrim"; s.onclick=closeDay;
  const p = document.createElement("aside"); p.className="panel"; p.setAttribute("role","dialog");
  p.setAttribute("aria-label","Roster for "+date);
  p.innerHTML = `<button class="close" aria-label="Close">&times;</button>
    <h2>${new Date(date+"T12:00").toLocaleDateString(undefined,{weekday:"long",month:"long",day:"numeric"})}</h2>
    <div class="psub">${d.holiday?"Observed holiday &middot; ":""}${d.weekend?"Weekend &middot; ":""}${working.size} physicians working</div>
    ${vs.length?`<div class="pgroup"><h4>Findings</h4>`+vs.map(v=>
      `<div class="pitem"><span class="d">${v.message}</span>
       <span class="sev ${v.severity}">${v.severity}</span></div>`).join("")+`</div>`:""}
    <div class="pgroup"><h4>Duties</h4>${list}</div>
    ${free.length?`<div class="pgroup"><h4>Not scheduled</h4><div class="free">`+
      free.map(x=>`<span class="pill">${x.name}</span>`).join("")+`</div></div>`:""}
    ${offs.length?`<div class="pgroup"><h4>Away</h4><div class="free">`+
      offs.map(x=>`<span class="pill offp">${x.name} &middot; ${OFF[date+"|"+x.id]}</span>`).join("")
      +`</div></div>`:""}`;
  p.querySelector(".close").onclick = closeDay;
  document.body.append(s,p); p.focus();
}
function closeDay(){ document.querySelectorAll(".scrim,.panel").forEach(n=>n.remove()); }
addEventListener("keydown", e=>{ if(e.key==="Escape") closeDay(); });

[...tabs.children].forEach(b=>b.setAttribute("aria-selected", b.dataset.v===view));
theme.textContent = (document.documentElement.dataset.theme==="dark") ? "Light" : "Dark";
render();
</script>
"""


def render_viewer(result: SolveResult) -> str:
    data = schedule_json(result)
    _short_codes(data["shifts"])
    return (TEMPLATE
            .replace("__DATA__", json.dumps(data, separators=(",", ":")))
            .replace("__GROUPS__", json.dumps(GROUP_LABELS))
            .replace("__TIERS__", json.dumps({str(k): v for k, v in TIER_LABELS.items()})))


def write_viewer(result: SolveResult, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(render_viewer(result))
