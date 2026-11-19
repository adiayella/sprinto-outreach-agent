"""Website reader: our own scraper (robots, limits, redirect checks, menu stripping).

In simulation mode pages come from fixtures/sites/<domain>.json instead of the network.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .ats import find_boards
from .settings import FIXTURES
from .urlguard import UnsafeURL, absolute, canonical, check_url, same_site

UA = "Mozilla/5.0 (compatible; SprintoResearchBot/1.0; +demo)"
MAX_BYTES = 2_000_000
TIMEOUT = 10.0
THIN_CHARS = 400

# Most valuable first. Words are matched in the path *and* the subdomain (trust.acme.com).
CATEGORIES = [
    ("security", ("security", "trust", "compliance", "soc-2", "soc2", "iso", "iso-27001", "gdpr", "privacy-center",
                  "trust-center", "hipaa")),
    ("pricing", ("pricing", "plans", "enterprise")),
    ("customers", ("customers", "customer-stories", "case-studies", "case-study", "clients", "testimonials")),
    ("careers", ("careers", "career", "jobs", "join-us", "hiring", "work-with-us", "join")),
    # funding announcements usually live in the newsroom or a blog post with "raises"/"series-x" in the URL
    ("press", ("press", "newsroom", "news", "announcement", "announcements", "announcing", "funding", "raises",
               "raised", "series-a", "series-b", "series-c", "series-d", "media")),
    ("company", ("about", "company", "team", "our-story")),
]


@dataclass
class Page:
    url: str
    ok: bool
    status: int = 0
    method: str = "http"
    text: str = ""
    title: str = ""
    links: list[dict] = field(default_factory=list)
    error: str = ""
    thin: bool = False
    category: str = "home"
    ats: list = field(default_factory=list)  # job boards referenced in the raw HTML: [(provider, token)]
    ext_hosts: list = field(default_factory=list)  # hosts this page links out to (identity checks on articles)


def categorise(url: str) -> str | None:
    """Match category words as whole words in the subdomain and path ("cypress" is not "press")."""
    p = urlsplit(url)
    hay = f"/{(p.hostname or '').split('.')[0]}/{p.path.lower()}/"
    tokens = set(re.split(r"[/\-_.]+", hay))
    in_blog = bool(tokens & {"blog", "blogs", "posts", "resources", "articles", "guides"})
    for cat, words in CATEGORIES:
        if in_blog and cat != "press":  # a blog post about "CI jobs" or "trust" is not a careers/security page
            continue
        for w in words:
            if "-" in w or "/" in w:
                if re.search(r"(?<![a-z0-9])" + re.escape(w.strip("/")) + r"(?![a-z0-9])", hay):
                    return cat
            elif w in tokens:
                return cat
    return None


def depth(url: str) -> int:
    return len([s for s in urlsplit(url).path.split("/") if s])


def html_to_page(url: str, html: str, domain: str, status: int = 200, method: str = "http") -> Page:
    boards = find_boards(html)  # before stripping: boards are usually in iframes/scripts/off-site links
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")[:200]
    links, seen, ext = [], set(), set()
    for a in soup.find_all("a", href=True):
        u = absolute(url, a["href"])
        if not u:
            continue
        u = canonical(u)
        if not same_site(u, domain):
            ext.add((urlsplit(u).hostname or "").removeprefix("www."))
            continue
        if u in seen:
            continue
        seen.add(u)
        links.append({"url": u, "text": a.get_text(" ", strip=True)[:80]})
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "form", "iframe"]):
        tag.decompose()
    for tag in soup.select('[role="navigation"], [aria-label*="menu" i], .cookie, #cookie-banner'):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)[:60_000]
    return Page(url=url, ok=True, status=status, method=method, text=text, title=title,
                links=links, thin=len(text) < THIN_CHARS, ats=boards, ext_hosts=sorted(ext)[:300])


class Fetcher:
    def __init__(self, domain: str, simulation: bool = False):
        self.domain = domain
        self.simulation = simulation
        self._robots: robotparser.RobotFileParser | None = None
        self._fixture: dict | None = None
        self._last = 0.0
        if simulation:
            f = FIXTURES / "sites" / f"{domain}.json"
            self._fixture = json.loads(f.read_text()) if f.exists() else {}

    # -- simulation ---------------------------------------------------------
    def _fixture_fetch(self, url: str, raw: bool = False) -> Page:
        path = urlsplit(url).path or "/"
        html = self._fixture.get(path) if self._fixture else None
        if html is None:
            return Page(url=url, ok=False, status=404, method="fixture", error="not found (fixture)")
        if raw:
            return Page(url=url, ok=True, status=200, method="fixture", text=html)
        return html_to_page(url, html, self.domain, method="fixture")

    # -- real ---------------------------------------------------------------
    def _allowed(self, client: httpx.Client, url: str) -> bool:
        if self._robots is None:
            rp = robotparser.RobotFileParser()
            try:
                r = client.get(f"https://{self.domain}/robots.txt", timeout=TIMEOUT)
                if r.status_code in (301, 302, 307, 308):  # e.g. apex -> www: follow one same-site hop
                    nxt = absolute(str(r.url), r.headers.get("location", ""))
                    if nxt and same_site(nxt, self.domain):
                        check_url(nxt)
                        r = client.get(nxt, timeout=TIMEOUT)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._robots = rp
        return self._robots.can_fetch(UA, url)

    def fetch(self, url: str, raw: bool = False) -> Page:
        """raw=True returns the body as-is (used for sitemap XML)."""
        url = canonical(url)
        if self.simulation:
            return self._fixture_fetch(url, raw)
        if not same_site(url, self.domain):
            return Page(url=url, ok=False, error="off-site URL refused")
        last_err = ""
        for attempt in range(3):
            try:
                return self._fetch_once(url, raw)
            except UnsafeURL as e:
                return Page(url=url, ok=False, error=f"blocked by URL guard: {e}")
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"{type(e).__name__}"
                time.sleep(0.8 * (attempt + 1))
        return Page(url=url, ok=False, error=f"network error after 3 attempts: {last_err}")

    def _fetch_once(self, url: str, raw: bool = False) -> Page:
        wait = 0.7 - (time.time() - self._last)  # polite per-host delay
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        with httpx.Client(headers={"User-Agent": UA, "Accept": "text/html,*/*"},
                          follow_redirects=False, timeout=TIMEOUT) as client:
            check_url(url)
            if not self._allowed(client, url):
                return Page(url=url, ok=False, error="disallowed by robots.txt")
            current, seen = url, set()
            for _ in range(6):
                check_url(current)
                seen.add(current)
                with client.stream("GET", current) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        nxt = absolute(current, r.headers.get("location", ""))
                        if not nxt or not same_site(nxt, self.domain):
                            boards = find_boards(nxt or "")
                            if boards:  # e.g. acme.com/jobs -> jobs.lever.co/acme: the company's own link to its board
                                return Page(url=url, ok=False, status=r.status_code, ats=boards,
                                            error=f"redirects to job board ({boards[0][0]})")
                            return Page(url=url, ok=False, status=r.status_code, error="cross-site redirect refused")
                        # Follow the exact target: canonical() folds www into the apex, which would loop forever on
                        # sites that redirect apex -> www. canonical() is only for de-duplication keys.
                        if nxt in seen:
                            return Page(url=url, ok=False, status=r.status_code, error="redirect loop")
                        current = nxt
                        continue
                    if r.status_code >= 400:
                        return Page(url=url, ok=False, status=r.status_code, error=f"HTTP {r.status_code}")
                    ctype = r.headers.get("content-type", "")
                    if "html" not in ctype and "text" not in ctype and not (raw and "xml" in ctype):
                        return Page(url=url, ok=False, status=r.status_code, error=f"not HTML ({ctype[:40]})")
                    body = b""
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    html = body.decode(r.encoding or "utf-8", errors="replace")
                    if raw:
                        return Page(url=canonical(current), ok=True, status=r.status_code, text=html)
                    page = html_to_page(current, html, self.domain, status=r.status_code)
                    page.url = canonical(current)
                    return page
            return Page(url=url, ok=False, error="too many redirects")

    def sitemap_urls(self, limit: int = 2000) -> tuple[list[str], str]:
        """Same-site URLs listed in /sitemap.xml (and up to 2 nested sitemaps). Returns (urls, note)."""
        top = self.fetch(f"https://{self.domain}/sitemap.xml", raw=True)
        if not top.ok:
            return [], f"no sitemap ({top.error or top.status})"
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", top.text)
        urls = [u for u in locs if not u.endswith(".xml")]
        nested = [u for u in locs if u.endswith(".xml")]
        # prefer nested sitemaps that sound like pages/news over product catalogues
        nested.sort(key=lambda u: (not re.search(r"page|post|news|blog|press|company", u), len(u)))
        for sm in nested[:2]:
            sub = self.fetch(sm, raw=True)
            if sub.ok:
                urls += [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sub.text) if not u.endswith(".xml")]
        urls = [canonical(u) for u in urls if same_site(u, self.domain)]
        return list(dict.fromkeys(urls))[:limit], f"sitemap listed {len(urls)} pages"

    def homepage(self) -> Page:
        p = self.fetch(f"https://{self.domain}/")
        if not p.ok and not self.simulation:
            p2 = self.fetch(f"https://www.{self.domain}/")
            if p2.ok:
                return p2
        return p


def pick_category_pages(links: list[dict], already: set[str], limit: int) -> list[tuple[str, str]]:
    """One page per category, most valuable category first; index pages beat deep pages."""
    best: dict[str, str] = {}
    for link in links:
        u = link["url"]
        if u in already:
            continue
        cat = categorise(u)
        if not cat:
            continue
        if cat not in best or (depth(u), len(u)) < (depth(best[cat]), len(best[cat])):
            best[cat] = u
    order = [c for c, _ in CATEGORIES]
    picked = [(best[c], c) for c in order if c in best]
    return picked[:limit]
