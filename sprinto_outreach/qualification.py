"""Rubric scored in code. The LLM proposes levels and cites evidence ids; code validates and decides."""
from __future__ import annotations

import re
from datetime import date

from .schemas import QualOut
from .settings import load_config

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def evidence_table(evidence: list[dict]) -> str:
    return "\n".join(f'{e["eid"]} [{e["field"]}; {e["source_type"]}; {e.get("status", "verified")}; '
                     f'{e["category"] or "page"}] "{e["excerpt"]}"' for e in evidence) or "(no evidence yet)"


def build_prompt(domain: str, fields: dict, evidence: list[dict], candidate_links: list[str], today: str) -> str:
    r = load_config("rubric_v1")
    dims = "\n".join(
        f'- {d["key"]} ({d["label"]}, weight {d["weight"]}): level 2 = {d["level_2"]}; level 1 = {d["level_1"]}; level 0 = not shown'
        for d in r["dimensions"])
    facts = "\n".join(f"- {k}: {v['value'] or 'unknown'} ({v['status']})" for k, v in fields.items())
    links = "\n".join(candidate_links[:40]) or "(none)"
    return f"""Score {domain} as a prospect for Sprinto (compliance automation: SOC 2, ISO 27001, GDPR) using ONLY the evidence below.
Today is {today}.

Rubric dimensions (level 0, 1 or 2 each):
{dims}

Rules:
- Every level above 0 must cite evidence ids from the table. No evidence -> level 0 with zero_reason "unknown".
- zero_reason "explicit_negative" only if evidence shows the opposite; "outside_window" if dated too long ago.
- reason_now: level 2 needs a security/GRC/compliance hiring post or audit/deadline within 6 months; level 1 a dated expansion
  (funding round, new region, enterprise launch) within 12 months. Put the stated date in evidence_date. Live job posts on the
  careers page count as current.
- Absence is not evidence: no SOC 2 badge does not mean no SOC 2.
- unrelated_business = true only when evidence shows they don't sell software or digital services.
- gaps: what missing facts would most change the score. next_pages: up to 2 URLs copied exactly from the candidate links that
  would most likely fill those gaps (security/trust, careers, customers, about). search_query: one web query for facts a
  company site rarely states (e.g. "<company> Series B funding 2026"), or empty.

Extracted facts:
{facts}

<untrusted_evidence>
{evidence_table(evidence)}
</untrusted_evidence>

Candidate links not yet read:
{links}"""


def _date_ok(evidence_date: str, cited: list[dict], months: int, today: date) -> tuple[bool, str]:
    """Check a stated date appears in the cited text and is inside the window. Live careers pages count as today."""
    if any(e["category"] == "careers" and e["source_type"] in ("company_site", "job_board") for e in cited):
        return True, ("live job post on the company's job board (retrieved today)"
                      if any(e["source_type"] == "job_board" for e in cited) else
                      "live job post on careers page (retrieved today)")
    m = re.match(r"(\d{4})-(\d{2})", evidence_date or "")
    if not m:
        return False, "no date stated"
    y, mo = int(m.group(1)), int(m.group(2))
    text = " ".join(e["excerpt"].lower() for e in cited)
    if str(y) not in text:
        return False, f"date {evidence_date} not found in cited evidence"
    age = (today.year - y) * 12 + (today.month - mo)
    if age < 0 or age > months:
        return False, f"dated {evidence_date}, outside {months}-month window"
    return True, f"dated {evidence_date}"


def score(out: QualOut, fields: dict, evidence: list[dict], domain: str, today: date) -> dict:
    r = load_config("rubric_v1")
    by_id = {e["eid"]: e for e in evidence}
    proposed = {d.key: d for d in out.dimensions}
    dims, total, best_case = [], 0.0, 0.0
    for spec in r["dimensions"]:
        k, w = spec["key"], spec["weight"]
        d = proposed.get(k)
        level = d.level if d else 0
        ids = [i for i in (d.evidence_ids if d else []) if i in by_id]
        dropped = [i for i in (d.evidence_ids if d else []) if i not in by_id]
        reason = d.reason if d else "not scored"
        zero = d.zero_reason if d else "unknown"
        note = ""
        if level > 0 and not ids:
            level, zero, note = 0, "unknown", "cited evidence ids don't exist — ignored"
        if level == 2 and ids and all(by_id[i].get("status") == "inferred" for i in ids):
            level, note = 1, "capped at 1: only third-party / unconfirmed sources support level 2"
        if k == "reason_now" and level > 0:
            ok, why = _date_ok(d.evidence_date, [by_id[i] for i in ids], 6 if level == 2 else 12, today)
            if not ok and level == 2:
                ok1, why1 = _date_ok(d.evidence_date, [by_id[i] for i in ids], 12, today)
                level, note = (1, f"downgraded: {why}") if ok1 else (0, why)
                if level == 0:
                    zero = "outside_window" if "outside" in why else "unknown"
            elif not ok:
                level, zero, note = 0, ("outside_window" if "outside" in why else "unknown"), why
            else:
                note = why
        if level == 0 and zero == "not_applicable":
            zero = "unknown"
        pts = w * level / 2
        total += pts
        best_case += pts if (level == 2 or zero in ("explicit_negative", "outside_window")) else w
        dims.append({"key": k, "label": spec["label"], "weight": w, "level": level, "points": pts,
                     "evidence_ids": ids, "dropped_ids": dropped, "reason": reason,
                     "zero_reason": zero if level == 0 else "", "note": note})

    lv = {d["key"]: d for d in dims}
    threshold = r["qualify_threshold"]
    name = (fields.get("company_name", {}).get("value") or "").lower()
    stage = current_stage(evidence)
    stage_note = ""
    needs_funding_check = False
    decision, why = "UNDECIDED", ""

    competitor = next((c for c in r["competitors"] if c in name or domain.split(".")[0] == c.replace(" ", "")), None)
    if competitor:
        decision, why = "DISQUALIFIED", f"direct competitor ({competitor})"
    elif out.unrelated_business and [i for i in out.unrelated_evidence_ids if i in by_id] and lv["fit"]["level"] == 0:
        decision, why = "DISQUALIFIED", "evidence shows an unrelated (non-software) business"
    elif stage and stage["rank"] >= 5:
        decision, why = "LOW_PRIORITY", (f"latest funding found is {stage['label']} ({stage['eid']}), past Sprinto's "
                                         f"Series A–C core ICP")
    elif stage and stage["rank"] <= 1:
        decision, why = "LOW_PRIORITY", (f"latest funding found is {stage['label']} ({stage['eid']}), before Sprinto's "
                                         f"Series A–C core ICP")
        # A company site often only mentions its first round. Companies only move up in stage, so one web search
        # for the latest round can overturn this — never rule out on a company-site seed mention alone.
        needs_funding_check = not any(e["source_type"] == "web_search" and e["field"] == "funding_stage" for e in evidence)
    elif total >= threshold and lv["fit"]["level"] == 2 and lv["workload"]["level"] >= 1:
        decision, why = "QUALIFIED", f"score {total:g} ≥ {threshold}, clear SaaS fit and a compliance workload"
    elif best_case < threshold:
        decision, why = "LOW_PRIORITY", f"even if every unknown came back at the best level the score would be {best_case:g} < {threshold}"
    elif lv["fit"]["zero_reason"] == "explicit_negative":
        decision, why = "LOW_PRIORITY", "evidence contradicts SaaS / data-handling fit"
    if stage and 2 <= stage["rank"] <= 4:
        stage_note = f"latest funding found is {stage['label']} ({stage['eid']}) — inside the Series A–C core ICP"
    elif not stage:
        stage_note = "funding stage not stated anywhere read — never inferred"
    return {"total": total, "best_case": best_case, "threshold": threshold, "dimensions": dims,
            "decision": decision, "why": why, "stage_note": stage_note, "gaps": out.gaps,
            "stage": stage, "needs_funding_check": needs_funding_check}


STAGES = [  # (pattern, rank, label) — checked in this order; pre-seed before seed
    (r"\bpre-?seed\b", 0, "Pre-seed"), (r"\bseed\b", 1, "Seed"),
    (r"\bseries a\b", 2, "Series A"), (r"\bseries b\b", 3, "Series B"), (r"\bseries c\b", 4, "Series C"),
    (r"\bseries [d-h]\b", 5, "Series D+"),
    (r"\b(ipo|nasdaq|nyse|publicly traded|public company)\b", 8, "Public"), (r"\bacquired by\b", 8, "Acquired"),
]


def current_stage(evidence: list[dict]) -> dict | None:
    """Highest funding stage stated in any funding evidence. Companies only move up, so the highest is the latest."""
    best = None
    for e in evidence:
        if e["field"] != "funding_stage" or e.get("status", "verified") != "verified":
            continue  # third-party estimates never decide funding stage on their own
        text = e["excerpt"].lower()
        for pat, rank, label in STAGES:
            if label == "Seed":
                text_wo = re.sub(r"\bpre-?seed\b", "", text)
                hit = re.search(pat, text_wo)
            else:
                hit = re.search(pat, text)
            if hit and (best is None or rank > best["rank"]):
                best = {"label": label, "rank": rank, "eid": e["eid"], "source_type": e["source_type"], "eids": [e["eid"]]}
            elif hit and rank == best["rank"] and e["eid"] not in best["eids"]:
                best["eids"].append(e["eid"])  # same stage confirmed by another source
    if best and len(best["eids"]) > 1:
        best["eid"] = ", ".join(best["eids"])
        best["source_type"] = "company site + web search" if any(
            x["source_type"] == "web_search" for x in evidence if x["eid"] in best["eids"]) else best["source_type"]
    return best
