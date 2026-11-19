"""Pain hypotheses: the LLM proposes up to 4, code re-scores each out of 10 and keeps the best two distinct ones."""
from __future__ import annotations

import re

from .qualification import evidence_table
from .schemas import PainsOut
from .settings import load_config

HEDGES = r"\b(likely|probably|may|might|could|often|perhaps|guess|wonder|imagine|tends to|usually|typically)\b"
CONSEQUENCES = r"\b(deal|deals|sales cycle|audit|auditor|roadmap|engineer|engineering time|questionnaire|review|renewal|customer|enterprise|risk|launch|expansion|hiring|evidence|controls?|framework)\b"
DISHONEST = r"(you('re| are) (not|non)[- ]compliant|non-compliant|failing|losing|lost revenue|urgent|immediately|\d+\s*%|\$\s?\d|guarantee)"
ROLE_CAPS = {
    "compliance": {"evidence_automation", "continuous_monitoring", "multi_framework", "policies_training"},
    "security": {"continuous_monitoring", "evidence_automation", "security_reviews", "multi_framework"},
    "cto": {"evidence_automation", "security_reviews", "audit_ready_fast", "continuous_monitoring"},
    "founder": {"audit_ready_fast", "security_reviews", "multi_framework"},
}


def build_prompt(company: str, contact: dict, fields: dict, evidence: list[dict], feedback: str = "") -> str:
    caps = load_config("capabilities_v1")
    cap_txt = "\n".join(f'- {c["id"]}: {c["text"]}' for c in caps["capabilities"])
    facts = "\n".join(f"- {k}: {v['value'] or 'unknown'}" for k, v in fields.items())
    return f"""Propose up to 4 hypotheses about the most likely compliance/security pains for this person.

Person: {contact['name']}, {contact['title']} at {company} (role group: {contact['role_label']}).

Rules:
- Each hypothesis must be grounded in cited evidence ids from the table (at least one, ideally two).
- Write the statement tentatively ("is probably", "may", "likely") — it is a guess, not a fact.
- Make it specific to this person's job and to what the evidence shows about the company.
- Name a real consequence (enterprise deals, audits, engineering time, security reviews, expansion).
- Map each to exactly one approved capability_id below. Do not claim anything else about Sprinto.
- No invented numbers, percentages, money, urgency, or claims that they are non-compliant.
- If the company already holds a certification, the pain is upkeep / adding frameworks, not getting certified.
{("Feedback on the previous attempt: " + feedback) if feedback else ""}

Approved capabilities:
{cap_txt}

Company facts:
{facts}

<untrusted_evidence>
{evidence_table(evidence)}
</untrusted_evidence>"""


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", s.lower())}


def score(out: PainsOut, evidence: list[dict], contact: dict) -> dict:
    caps = {c["id"] for c in load_config("capabilities_v1")["capabilities"]}
    pass_score = load_config("policy_v1")["pain_pass_score"]
    ids = {e["eid"] for e in evidence}
    ev_text = " ".join(e["excerpt"].lower() for e in evidence)
    scored = []
    for h in out.hypotheses[:4]:
        valid = [i for i in h.evidence_ids if i in ids]
        s = {}
        s["evidence"] = 0 if not valid else (2 if len(valid) == 1 else 3)
        s["relevance"] = 2 if h.capability_id in ROLE_CAPS.get(contact.get("role") or "", set()) else 1
        hits = len(set(re.findall(CONSEQUENCES, h.statement.lower())))
        s["consequence"] = 2 if hits >= 2 else (1 if hits == 1 else 0)
        s["capability"] = 2 if h.capability_id in caps else 0
        bad = re.search(DISHONEST, h.statement.lower())
        nums_unsupported = [n for n in re.findall(r"\d[\d,.]*", h.statement) if n not in ev_text]
        s["honest"] = 1 if (re.search(HEDGES, h.statement.lower()) and not bad and not nums_unsupported) else 0
        total = sum(s.values())
        scored.append({**h.model_dump(), "evidence_ids": valid, "scores": s, "total": total,
                       "passed": total >= pass_score and s["evidence"] >= 2 and s["honest"] == 1})
    scored.sort(key=lambda x: -x["total"])
    chosen = []
    for h in scored:
        if not h["passed"]:
            continue
        if any(h["capability_id"] == c["capability_id"] or
               len(_words(h["statement"]) & _words(c["statement"])) / max(1, len(_words(h["statement"]))) > 0.6
               for c in chosen):
            continue
        chosen.append(h)
        if len(chosen) == 2:
            break
    return {"all": scored, "chosen": chosen, "ok": len(chosen) == 2}
