"""Rule-based stand-in for Gemini, used only in simulation mode (no network, no key).

It proves the pipeline, the guards and the UI — not prompt quality.
"""
from __future__ import annotations

import json
import re

from .schemas import (Citation, DimOut, EmailEval, EmailParts, ExtractionOut, FieldOut, PainOut, PainsOut, QualOut)
from .settings import FIXTURES

PATTERNS = {
    "product": r"\b(platform|software|product|suite|bakery)\b",
    "business_model": r"\b(saas|subscription|cloud service|cloud)\b",
    "customer_segments": r"\b(enterprise|banks?|insurers?|hospital|clinics|engineering teams|finance teams|fortune 500)\b",
    "named_customers": r"\b(trusted by|customers include|used by)\b",
    "funding_stage": r"\b(series [a-h]|seed|raised|publicly traded|nyse|nasdaq)\b",
    "employee_count": r"\b(\d[\d,]*\+? (people|employees)|team of \d+|more than [\d,]+ people)\b",
    "data_handled": r"\b(sensitive|personal data|patient|financial data|customer data|user targeting data|customer configuration)\b",
    "compliance_claims": r"\b(soc ?2|iso ?27001|gdpr|hipaa|pci dss|fedramp)\b",
    "hiring_signals": r"\b(open roles|hiring|grc analyst|security engineer|compliance manager)\b",
    "business_triggers": r"\b(expand|launch|announced|series [a-h]|raised)\b",
    "existing_compliance_tools": r"\b(vanta|drata|secureframe|onetrust)\b",
}


def _lines(text: str) -> list[str]:
    return [l.strip() for l in text.split("\n") if len(l.strip()) > 12]


class SimulatedLLM:
    simulated = True
    calls = 0

    def status(self) -> dict:
        return {"chain": ["simulated-rule-based"], "holds": {}, "search_paused": False}

    def generate_json(self, task: str, prompt: str, schema, context: dict | None = None):
        self.calls += 1
        ctx = context or {}
        return getattr(self, f"_{task}")(ctx)

    def grounded_search(self, query: str, context: dict | None = None) -> dict:
        self.calls += 1
        domain = (context or {}).get("domain", "")
        f = FIXTURES / "sites" / f"{domain}.json"
        statements = json.loads(f.read_text()).get("__search__", []) if f.exists() else []
        return {"text": " ".join(s["text"] for s in statements),
                "sources": [{"title": s["source_title"], "uri": s["source_uri"]} for s in statements],
                "statements": statements, "model": "simulated"}

    # ---- tasks ------------------------------------------------------------
    def _extract(self, ctx) -> ExtractionOut:
        out = []
        pages = ctx["pages"]
        home = next((p for p in pages if p.get("category") == "home"), None)
        if home:
            first = _lines(home["text"])[0]
            name = re.split(r" [—|-] ", first)[0]
            out.append(FieldOut(field="company_name", status="observed", value=name,
                                citations=[Citation(page=home["pid"], excerpt=first)]))
        for field, pat in PATTERNS.items():
            cites, vals = [], []
            for p in pages:
                for line in _lines(p["text"]):
                    m = re.search(pat, line, re.I)
                    if m and len(cites) < 3:
                        cites.append(Citation(page=p["pid"], excerpt=line))
                        vals.append(m.group(0))
            if cites:
                out.append(FieldOut(field=field, status="observed", value=", ".join(dict.fromkeys(vals)), citations=cites))
        # one deliberately bad citation, to show the validator rejecting it
        out.append(FieldOut(field="business_triggers", status="inferred", value="(hallucination test)",
                            citations=[Citation(page=pages[0]["pid"], excerpt="The company plans an IPO next quarter.")]))
        return ExtractionOut(fields=out)

    def _qualify(self, ctx) -> QualOut:
        ev = ctx["evidence"]

        def find(fields, pat, cat=None):
            return [e["eid"] for e in ev if e["field"] in fields and re.search(pat, e["excerpt"], re.I)
                    and (cat is None or e["category"] == cat)]

        dims = []
        unrelated = find(["product", "company_name"], r"bakery|restaurant|cafe")
        fit = find(["business_model", "product"], r"saas|cloud|platform")
        dims.append(DimOut(key="fit", level=2 if fit and not unrelated else 0, evidence_ids=fit[:2],
                           reason="States a SaaS / cloud platform handling customer data" if fit else "No software product found",
                           zero_reason="explicit_negative" if unrelated else "unknown"))
        ent = find(["customer_segments", "named_customers"], r"enterprise|bank|insur|hospital|fortune")
        dims.append(DimOut(key="trust_exposure", level=2 if ent else (1 if fit else 0), evidence_ids=ent[:2] or fit[:1],
                           reason="Enterprise plan or regulated customers" if ent else "B2B, no enterprise signal yet"))
        comp = find(["compliance_claims"], r"soc|iso|gdpr|hipaa|pci")
        data = find(["data_handled"], r".")
        dims.append(DimOut(key="workload", level=2 if comp else (1 if data else 0), evidence_ids=(comp or data)[:2],
                           reason="Named frameworks / audits in progress" if comp else "Handles sensitive data, no framework found yet"))
        hire = find(["hiring_signals"], r"grc|security|compliance", cat="careers")
        fund = find(["funding_stage", "business_triggers"], r"(january|february|march|april|may|june|july|august|september|october|november|december) 20\d\d")
        if hire:
            dims.append(DimOut(key="reason_now", level=2, evidence_ids=hire[:2], reason="Hiring for security/compliance now"))
        elif fund:
            txt = next(e["excerpt"] for e in ev if e["eid"] == fund[0])
            m = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december) (20\d\d)", txt, re.I)
            month = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
                     "october", "november", "december"].index(m.group(1).lower()) + 1
            dims.append(DimOut(key="reason_now", level=1, evidence_ids=fund[:1], reason="Recent dated funding round",
                               evidence_date=f"{m.group(2)}-{month:02d}"))
        else:
            dims.append(DimOut(key="reason_now", level=0, reason="No dated trigger found", zero_reason="unknown"))
        fw = {re.sub(r"\s", "", m.lower()) for e in ev if e["field"] == "compliance_claims"
              for m in re.findall(r"soc ?2|iso ?27001|gdpr|hipaa|pci", e["excerpt"], re.I)}
        dims.append(DimOut(key="complexity", level=2 if len(fw) >= 2 else (1 if fw else 0), evidence_ids=comp[:2],
                           reason=f"Frameworks in scope: {', '.join(sorted(fw)) or 'none found'}"))
        gaps, next_pages, query = [], [], ""
        if not comp:
            gaps.append("No security/compliance page read yet")
        if not hire:
            gaps.append("No current hiring signal")
        funding_known = any(e["field"] == "funding_stage" for e in ev)
        if not funding_known:
            gaps.append("Funding stage not stated")
            query = f"{ctx['domain'].split('.')[0]} funding round series"
        for u in ctx["links"]:
            if re.search(r"trust|security|careers|jobs", u) and len(next_pages) < 2:
                next_pages.append(u)
        if any(e["source_type"] == "web_search" for e in ev):
            query = ""
        return QualOut(dimensions=dims, unrelated_business=bool(unrelated), unrelated_evidence_ids=unrelated[:1],
                       gaps=gaps, next_pages=next_pages, search_query=query)

    def _pains(self, ctx) -> PainsOut:
        ev, c = ctx["evidence"], ctx["contact"]
        company = ctx["company"]
        comp = [e["eid"] for e in ev if e["field"] == "compliance_claims"]
        ent = [e["eid"] for e in ev if e["field"] in ("customer_segments", "named_customers")]
        hire = [e["eid"] for e in ev if e["field"] == "hiring_signals"]
        data = [e["eid"] for e in ev if e["field"] == "data_handled"]
        hs = [
            PainOut(title="Audit evidence collection",
                    statement=f"With a SOC 2 audit in progress, collecting and refreshing audit evidence across tools is probably pulling "
                              f"engineering time away from the roadmap at {company}.",
                    evidence_ids=(comp[:2] + hire[:1])[:3], capability_id="evidence_automation",
                    why_this_person=f"As {c['title']}, {c['first_name']} likely owns the audit timeline."),
            PainOut(title="Enterprise security reviews",
                    statement="Enterprise and regulated customers likely send long security questionnaires, which may slow deals "
                              "while the review is answered by hand.",
                    evidence_ids=(ent[:2] + data[:1])[:3], capability_id="security_reviews",
                    why_this_person="Security reviews usually land on this person's desk."),
            PainOut(title="Multiple frameworks",
                    statement="Running several frameworks side by side could mean repeating the same controls work for each audit.",
                    evidence_ids=comp[:2], capability_id="multi_framework", why_this_person="Owns the control set."),
        ]
        return PainsOut(hypotheses=hs)

    def _email(self, ctx) -> EmailParts:
        ev, c, company = ctx["evidence"], ctx["contact"], ctx["company"]
        hire = next((e for e in ev if e["field"] == "hiring_signals" and re.search(r"grc|compliance|security", e["excerpt"], re.I)), None)
        comp = next((e for e in ev if e["field"] == "compliance_claims"), None)
        hook = hire or comp
        if hook is hire and hire:
            role = re.split(r" [—-] ", hire["excerpt"].replace("Open roles: ", ""))[0]
            opening = f"I noticed {company} is hiring a {role}, which usually means an audit is close."
        else:
            opening = f"I saw on your trust page that {company} is working through a SOC 2 audit right now."
        return EmailParts(
            subject=f"{company}'s audit prep",
            greeting=f"Hi {c['first_name']},",
            opening_fact=opening,
            opening_evidence_ids=[hook["eid"]] if hook else [],
            paragraphs=[
                "My guess is that a lot of the evidence collection still runs through your team by hand, and enterprise "
                "security reviews probably keep landing in the middle of it.",
                "Sprinto connects to your cloud, HR and code tools, collects that audit evidence automatically and keeps a "
                "trust centre current for enterprise deals, so the team can stay on the roadmap.",
            ],
            question="Is audit prep something you're trying to take off the team's plate this quarter?",
            capability_ids=["evidence_automation", "security_reviews"],
        )

    def _evaluate_email(self, ctx) -> EmailEval:
        return EmailEval(hook=2, hypothesis=2, value=2, next_step=2, clarity=1,
                         notes="Specific hook and a single clear question. Could be a little shorter.")
