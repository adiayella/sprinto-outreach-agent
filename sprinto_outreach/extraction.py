"""Fixed-field extraction -> validated evidence. A fact survives only if its excerpt is in the page text."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .schemas import FIELDS, ExtractionOut

FUNDING_WORDS = re.compile(
    r"\b(series [a-h]|seed|pre-seed|raised|funding|funded|backed by|ipo|nasdaq|nyse|acquired by|publicly traded)\b", re.I)
STAGE_RE = re.compile(r"\b(pre-seed|seed|series [a-h]|ipo|nasdaq|nyse|publicly traded|acquired)\b", re.I)


LEGAL_SUFFIX = re.compile(r"[,\s]+(inc|incorporated|llc|ltd|limited|gmbh|corp|corporation|co|plc|pvt|private limited|"
                          r"technologies|labs inc)\.?$", re.I)


def clean_company_name(name: str) -> str:
    """'Cal.com, Inc.' -> 'Cal.com' · 'Acme Ltd' -> 'Acme'."""
    name = (name or "").split(";")[0].strip()
    prev = None
    while prev != name:
        prev, name = name, LEGAL_SUFFIX.sub("", name).strip(" ,")
    return name


def norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def build_prompt(pages: list[dict], known: dict) -> str:
    known_txt = "\n".join(f"- {k}: {v['value']} ({v['status']})" for k, v in known.items() if v.get("status") != "unknown")
    blocks = "\n\n".join(
        f'<untrusted_page id="{p["pid"]}" url="{p["url"]}" source="{p.get("source_type", "company_site")}">\n'
        f'{p["text"][:7000]}\n</untrusted_page>' for p in pages)
    return f"""Extract facts about the company from the pages below. Return one entry for EVERY field:
{", ".join(FIELDS)}.

Rules:
- status "observed" = the page states it; "inferred" = strongly implied; "unknown" = not stated; "conflicting" = pages disagree.
- Each non-unknown field needs citations whose excerpt is copied WORD FOR WORD from the page (5-40 words).
- funding_stage: only if a page states it (e.g. "Series B", "raised", "seed"). Never infer stage from site design or size.
- compliance_claims: keep the exact wording ("SOC 2 Type II report" is different from "working towards SOC 2"). A missing badge is NOT evidence of no compliance.
- named_customers: only customers named on the page.
- hiring_signals: open roles, especially security, compliance, GRC, IT, enterprise sales.
- existing_compliance_tools: tools like Vanta, Drata, Secureframe, OneTrust if mentioned.
- compliance_claims: only the company's OWN certifications/audits — not frameworks it helps customers with.
- Pages with source="publication" or "third_party" are about many companies: cite only sentences that name this
  company. Pages with source="job_board" list open roles: use them for hiring_signals.

Already known (for context; add new evidence if you find it):
{known_txt or "- nothing yet"}

{blocks}"""


CUSTOMER_FRAMING = re.compile(
    r"\b(help(s|ing)?|enable(s)?|empower(s)?|let(s)?|allow(s)?)\b[^.]{0,60}\b(customers?|clients?|you|your|teams|businesses|"
    r"companies|organi[sz]ations|startups)\b"
    r"|\b(your|customers'?|clients'?)\s+(own\s+)?(soc ?2|iso|gdpr|hipaa|compliance|audit)"
    r"|\bfor your\b[^.]{0,40}\b(compliance|audit)"
    r"|\b(get|become|achieve|stay)\s+(soc ?2|iso ?27001|gdpr|hipaa|compliant)", re.I)
OWN_FRAMING = re.compile(r"\b(we|we're|we are|we have|we've|our|us)\b|\bis (soc ?2|iso|hipaa|gdpr)"
                         r"|\bhas (achieved|completed|obtained|received|passed)", re.I)
LEGAL_PAGE = re.compile(r"/(privacy|terms|cookie|cookies|legal|dpa|eula|tos|subprocessors?)(/|$|-)", re.I)
PRODUCT_FIELDS = {"product", "business_model", "customer_segments", "named_customers", "data_handled"}


def is_customer_compliance(quote: str) -> bool:
    """True when the quote is about the compliance the company sells/helps with, not its own."""
    return bool(CUSTOMER_FRAMING.search(quote)) and not OWN_FRAMING.search(quote)


def _names_company(text: str, company: str, domain: str) -> bool:
    t = text.lower()
    base = domain.split(".")[0]
    name = (company or "").lower().split(";")[0].strip()
    return (len(base) > 2 and base in t) or (len(name) > 2 and name in t) or domain in t


def validate(out: ExtractionOut, pages: list[dict], start_index: int, round_no: int,
             company: str = "", domain: str = "") -> tuple[dict, list[dict], list[str]]:
    """Return (fields, evidence, rejected_notes).

    Every citation must appear word for word in its page. Then, by source:
      tier 1 (company site / its job board) -> verified · tier 2 (credible press) -> verified if it names the company
      and links to/mentions its domain, else inferred · tier 3 (databases) -> inferred · anything else -> rejected.
    Compliance quotes about customers ("helps teams get SOC 2") and product claims from legal pages are rejected.
    """
    by_pid = {p["pid"]: p for p in pages}
    normed = {p["pid"]: norm(p["text"]) for p in pages}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fields: dict = {}
    evidence: list[dict] = []
    rejected: list[str] = []
    n = start_index
    for f in out.fields:
        if f.field not in FIELDS:
            continue
        valid_ids = []
        for c in f.citations:
            ex = norm(c.excerpt)
            if len(ex) < 8:
                continue
            pid = c.page if c.page in normed and ex in normed[c.page] else next(
                (k for k, t in normed.items() if ex in t), None)
            if not pid:
                rejected.append(f"{f.field}: excerpt not found in page text — \"{c.excerpt[:80]}\"")
                continue
            page = by_pid[pid]
            stype = page.get("source_type", "company_site")
            status, flags = "verified", []
            if stype in ("search_lead",):
                rejected.append(f"{f.field}: search summaries are leads, not evidence — \"{c.excerpt[:60]}\"")
                continue
            if f.field == "funding_stage" and not FUNDING_WORDS.search(c.excerpt):
                rejected.append(f"funding_stage: excerpt does not state funding — \"{c.excerpt[:80]}\"")
                continue
            if stype in ("company_site",) and f.field in PRODUCT_FIELDS and LEGAL_PAGE.search(page["url"]):
                rejected.append(f"{f.field}: legal/privacy page describes data handling, not the product — "
                                f"\"{c.excerpt[:60]}\"")
                continue
            if f.field == "compliance_claims" and is_customer_compliance(c.excerpt):
                rejected.append(f"compliance_claims: about customers' compliance, not the company's own — "
                                f"\"{c.excerpt[:70]}\"")
                continue
            if stype in ("publication", "third_party"):
                if domain and not _names_company(c.excerpt, company, domain):
                    rejected.append(f"{f.field}: third-party quote doesn't name the company — \"{c.excerpt[:70]}\"")
                    continue
                if domain and not page.get("links_company", False):
                    flags.append("identity_unconfirmed")
                if stype == "third_party" or flags:
                    status = "inferred"
            n += 1
            eid = f"E{n}"
            evidence.append({
                "eid": eid, "field": f.field, "url": page["url"], "excerpt": c.excerpt.strip(),
                "source_type": stype, "category": page.get("category", ""), "status": status, "flags": flags,
                "retrieved_at": now, "round": round_no,
            })
            valid_ids.append(eid)
        verified = [i for i in valid_ids if next(e for e in evidence if e["eid"] == i)["status"] == "verified"]
        status = f.status if valid_ids else "unknown"
        if valid_ids and not verified and status == "observed":
            status = "inferred"  # only third-party / unconfirmed sources say so
        value = f.value.strip() if valid_ids else ""
        if f.field == "funding_stage" and valid_ids:
            m = STAGE_RE.search(" ".join(e["excerpt"] for e in evidence if e["eid"] in valid_ids))
            value = m.group(1).title() if m else value
        fields[f.field] = {"status": status, "value": value, "evidence_ids": valid_ids}
    for k in FIELDS:
        fields.setdefault(k, {"status": "unknown", "value": "", "evidence_ids": []})
    return fields, evidence, rejected


def merge(old: dict, new: dict) -> dict:
    out = dict(old)
    for k, nv in new.items():
        ov = old.get(k)
        if not ov or ov["status"] == "unknown":
            out[k] = nv
        elif nv["status"] != "unknown":
            ids = ov["evidence_ids"] + [i for i in nv["evidence_ids"] if i not in ov["evidence_ids"]]
            status = "conflicting" if "conflicting" in (ov["status"], nv["status"]) else ov["status"]
            if k == "funding_stage" and nv["value"] and ov["value"] and nv["value"].lower() != ov["value"].lower():
                status = "conflicting"
            value = ov["value"] if nv["value"].lower() in ov["value"].lower() else f'{ov["value"]}; {nv["value"]}'
            out[k] = {"status": status, "value": value[:400], "evidence_ids": ids}
    return out
