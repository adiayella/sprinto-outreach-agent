"""Point-of-contact selection: Hunter Domain Search (or a manual Apollo entry), scored in code.

Hunter emails are research data only. They never reach the delivery code.
"""
from __future__ import annotations

import json
import re

import httpx

from .settings import FIXTURES, load_config

HUNTER = "https://api.hunter.io/v2/domain-search"

ROLE_PATTERNS = {
    "compliance": r"\b(compliance|grc|governance|risk|audit|privacy officer|dpo|data protection)\b",
    "security": r"\b(ciso|security|infosec|information security|trust)\b",
    "cto": r"\b(cto|chief technology|vp,? (of )?engineering|vice president,? (of )?engineering|head of engineering|engineering director|director of engineering|chief information officer|cio|head of (it|infrastructure|platform)|vp,? (of )?(it|infrastructure))\b",
    "founder": r"\b(founder|co-founder|cofounder|ceo|chief executive)\b",
}
ROLE_LABEL = {"compliance": "Compliance / GRC lead", "security": "Security lead / CISO",
              "cto": "CTO / VP Engineering", "founder": "Founder / CEO"}
SENIOR = r"\b(chief|vp|vice president|head|director|founder|co-founder|ceo|cto|ciso|cio|lead|principal)\b"


class HunterError(Exception):
    def __init__(self, msg: str, kind: str):
        super().__init__(msg)
        self.kind = kind  # auth | quota | unavailable


def role_of(title: str) -> str | None:
    t = (title or "").lower()
    for role in ("compliance", "security", "cto", "founder"):
        if re.search(ROLE_PATTERNS[role], t):
            return role
    return None


def hunter_search(domain: str, key: str, simulation: bool) -> dict:
    if simulation:
        data = json.loads((FIXTURES / "hunter.json").read_text())
        return data.get(domain, {"data": {"emails": [], "organization": None}, "meta": {"results": 0}})
    if not key:
        raise HunterError("HUNTER_API_KEY is not set", "auth")
    params = {"domain": domain, "api_key": key, "limit": 10, "type": "personal"}
    try:
        r = httpx.get(HUNTER, params=params, timeout=20)
    except httpx.HTTPError as e:
        raise HunterError(f"Hunter unreachable: {type(e).__name__}", "unavailable") from e
    if r.status_code == 401:
        raise HunterError("Hunter rejected the API key", "auth")
    if r.status_code in (429, 403) and "limit" in r.text.lower() or r.status_code == 429:
        raise HunterError("Hunter monthly search quota used up", "quota")
    if r.status_code >= 400:
        raise HunterError(f"Hunter error {r.status_code}: {r.text[:200]}", "unavailable")
    return r.json()


def score_candidate(c: dict, domain: str) -> dict:
    title = c.get("position") or ""
    email = (c.get("value") or c.get("email") or "").lower()
    role = role_of(title)
    conf = c.get("confidence") or 0
    manual = c.get("source") == "manual"
    s = {}
    # 1. company association
    if manual:
        s["association"] = 2
    elif email.endswith("@" + domain) or email.endswith("." + domain):
        s["association"] = 2 if conf >= 70 else 1
    else:
        s["association"] = 0
    # 2. functional ownership of compliance/security
    s["ownership"] = {"compliance": 2, "security": 2, "cto": 2, "founder": 1}.get(role, 0)
    # 3. ability to drive action
    seniority = (c.get("seniority") or "").lower()
    if seniority == "executive" or re.search(r"\b(chief|vp|vice president|founder|ceo|cto|ciso|cio)\b", title.lower()):
        s["drive"] = 2
    elif seniority == "senior" or re.search(SENIOR, title.lower()):
        s["drive"] = 1
    else:
        s["drive"] = 0
    # 4. need match (does Sprinto's value land with this role?)
    s["need"] = {"compliance": 2, "security": 2, "cto": 2, "founder": 2}.get(role, 0)
    # 5. identity quality
    has_name = bool(c.get("first_name") and c.get("last_name"))
    if has_name and title and (manual or c.get("type", "personal") == "personal"):
        s["identity"] = 2
    elif has_name:
        s["identity"] = 1
    else:
        s["identity"] = 0
    total = sum(s.values())
    pol = load_config("policy_v1")
    passed = total >= pol["contact_pass_score"] and s["association"] > 0 and s["ownership"] > 0
    why = []
    if not role:
        why.append("title doesn't own security/compliance")
    if s["association"] == 0:
        why.append("not verifiably at this company")
    if s["identity"] < 2:
        why.append("incomplete identity (name/title)")
    return {
        "name": f'{c.get("first_name") or ""} {c.get("last_name") or ""}'.strip(),
        "first_name": c.get("first_name") or "", "last_name": c.get("last_name") or "",
        "title": title, "email": email, "confidence": conf, "linkedin": c.get("linkedin") or "",
        "source": "manual (Apollo)" if manual else "Hunter Domain Search",
        "role": role, "role_label": ROLE_LABEL.get(role, "Other"),
        "scores": s, "total": total, "passed": passed, "fail_reasons": why,
        "sources_count": len(c.get("sources") or []),
    }


def select(candidates: list[dict], founder_led_ok: bool = True) -> tuple[dict | None, str]:
    order = load_config("policy_v1")["contact_role_order"]
    passing = [c for c in candidates if c["passed"]]
    for role in order:
        if role == "founder" and not founder_led_ok:
            continue
        group = sorted([c for c in passing if c["role"] == role], key=lambda c: (-c["total"], -c["confidence"]))
        if group:
            chosen = group[0]
            skipped = [r for r in order[:order.index(role)]]
            why = (f"{chosen['name']} ({chosen['title']}) scored {chosen['total']}/10 — the highest-priority role available. "
                   f"Preference order: {' → '.join(ROLE_LABEL[r] for r in order)}.")
            if skipped:
                why += f" No passing {', '.join(ROLE_LABEL[r] for r in skipped)} was found."
            return chosen, why
    return None, "No candidate reached 8/10 with a security/compliance-owning role — no contact invented."
