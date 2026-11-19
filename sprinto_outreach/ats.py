"""Job boards (ATS) linked from a company's own pages: Greenhouse, Lever, Ashby, Workable.

Most careers pages embed one of these, so the open roles never appear in the page text. We only follow boards
*linked from the company's own site* (never guessed), read them through the providers' public job-board APIs,
and turn them into a plain-text page that extraction can quote word for word.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

from .settings import FIXTURES
from .urlguard import check_url

PATTERNS = {
    "greenhouse": r"(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|v1/boards/)?([A-Za-z0-9_-]+)",
    "lever": r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)",
    "ashby": r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([A-Za-z0-9_.%-]+)",
    "workable": r"apply\.workable\.com/(?:api/v\d/widget/accounts/)?([A-Za-z0-9_-]+)",
}
NOT_TOKENS = {"embed", "js", "v1", "api", "jobs", "j", "careers", "widget", "static", "assets"}
API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{t}",
}
PUBLIC = {
    "greenhouse": "https://job-boards.greenhouse.io/{t}",
    "lever": "https://jobs.lever.co/{t}",
    "ashby": "https://jobs.ashbyhq.com/{t}",
    "workable": "https://apply.workable.com/{t}",
}
RELEVANT = re.compile(r"\b(security|grc|compliance|governance|risk|audit|privacy|trust|ciso|it (manager|admin)|"
                      r"enterprise (account|sales)|soc ?2|iso ?27001)\b", re.I)


def find_boards(html: str) -> list[tuple[str, str]]:
    """(provider, token) for every job board referenced in the raw HTML (links, iframes, scripts)."""
    out: list[tuple[str, str]] = []
    for provider, pat in PATTERNS.items():
        for m in re.finditer(pat, html or ""):
            token = m.group(1).strip(".-").split("?")[0]
            if token and token.lower() not in NOT_TOKENS and (provider, token) not in out:
                out.append((provider, token))
    return out[:3]


def _date(v) -> str:
    if not v:
        return ""
    if isinstance(v, (int, float)):  # Lever: epoch ms
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date().isoformat()
    return str(v)[:10]


def _normalise(provider: str, data) -> list[dict]:
    jobs = []
    if provider == "greenhouse":
        for j in data.get("jobs", []):
            jobs.append({"title": j.get("title", ""), "location": (j.get("location") or {}).get("name", ""),
                         "department": "", "posted": _date(j.get("first_published") or j.get("updated_at"))})
    elif provider == "lever":
        for j in data if isinstance(data, list) else []:
            cat = j.get("categories") or {}
            jobs.append({"title": j.get("text", ""), "location": cat.get("location", ""),
                         "department": cat.get("team", ""), "posted": _date(j.get("createdAt"))})
    elif provider == "ashby":
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            jobs.append({"title": j.get("title", ""), "location": j.get("location", ""),
                         "department": j.get("department", ""), "posted": _date(j.get("publishedAt"))})
    elif provider == "workable":
        for j in data.get("jobs", []):
            loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
            jobs.append({"title": j.get("title", ""), "location": loc, "department": j.get("department", ""),
                         "posted": _date(j.get("published_on"))})
    return [j for j in jobs if j["title"]]


def fetch_board(provider: str, token: str, simulation: bool = False) -> dict:
    """{"url": public board URL, "jobs": [...], "text": quotable text, "relevant": [...] } — raises on failure."""
    if simulation:
        data = json.loads((FIXTURES / "ats.json").read_text()).get(f"{provider}:{token}")
        if data is None:
            raise LookupError("no such board (fixture)")
    else:
        url = API[provider].format(t=token)
        check_url(url)
        r = httpx.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; SprintoResearchBot/1.0)"})
        if r.status_code != 200:
            raise LookupError(f"{provider} board '{token}' returned HTTP {r.status_code}")
        data = r.json()
    jobs = _normalise(provider, data)[:150]
    lines = [f"Open role: {j['title']}" + (f" — {j['department']}" if j["department"] else "")
             + (f" — {j['location']}" if j["location"] else "") + (f" (posted {j['posted']})" if j["posted"] else "")
             for j in jobs]
    relevant = [j["title"] for j in jobs if RELEVANT.search(f"{j['title']} {j['department']}")]
    header = f"{len(jobs)} open roles on the {provider.title()} job board linked from the company's careers page."
    return {"url": PUBLIC[provider].format(t=token), "jobs": jobs, "relevant": relevant,
            "text": "\n".join([header, *lines]) if jobs else header}
