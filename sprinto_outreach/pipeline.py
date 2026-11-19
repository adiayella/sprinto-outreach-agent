"""The outreach stage machine. State is saved after every stage, so a crash or quota pause resumes where it stopped.

QUEUED -> RESEARCHING (steps 1-3, bounded loop) -> QUALIFIED -> CONTACT_SELECTED -> PAIN_READY -> DRAFT_READY -> DONE
"""
from __future__ import annotations

import re
import threading
import time
import traceback

import httpx
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import contacts as C
from . import email_gen as EG
from . import extraction as EX
from . import mailer
from . import ats as ATS
from . import pains as PN
from . import sources as SRC
from . import qualification as Q
from .fetcher import CATEGORIES, Fetcher, categorise, pick_category_pages
from .llm import LLMError, get_llm
from .schemas import EmailEval, EmailParts, ExtractionOut, PainsOut, QualOut
from .settings import Settings, load_config, versions
from .store import Store
from .urlguard import UnsafeURL, check_url

STEPS = {1: "Scrape website", 2: "Extract facts", 3: "Qualify vs ICP", 4: "Find contact",
         5: "Pain points", 6: "Draft email", 7: "Send"}
TERMINAL = {"SENT", "DRY_RUN_RECORDED", "SENT_SIMULATED", "DISQUALIFIED", "LOW_PRIORITY",
            "INSUFFICIENT_EVIDENCE", "NO_SUITABLE_CONTACT", "PAIN_EVIDENCE_THIN", "EMAIL_QA_FAILED"}
PAUSED = {"NEEDS_SETUP", "WAITING_QUOTA", "NEEDS_HUMAN", "SEND_UNCERTAIN"}
SERVERLESS_BUDGET_S = 25
ASSET_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp|pdf|zip|mp4|css|js|xml|ico)$", re.I)


def resolve_redirect(uri: str, simulation: bool = False) -> str | None:
    """Gemini grounding cites vertexaisearch redirect links; follow one hop to the real article URL."""
    if simulation or "vertexaisearch.cloud.google.com" not in uri:
        return uri or None
    try:
        check_url(uri)
        r = httpx.get(uri, follow_redirects=False, timeout=10)
    except (httpx.HTTPError, UnsafeURL):
        return None
    loc = r.headers.get("location", "")
    return loc if loc.startswith(("http://", "https://")) else None


class Pause(Exception):
    def __init__(self, outcome: str, reason: str):
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason


class Yield(Exception):
    """Serverless time budget used up: progress is saved and the next request carries on from here."""


class Stop(Exception):
    def __init__(self, outcome: str, reason: str):
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason


class Pipeline:
    def __init__(self, settings: Settings):
        self.s = settings
        self.stores: dict[str, Store] = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prospect")
        self._tl = threading.local()  # per-request deadline (serverless instances serve requests concurrently)

    def _checkpoint(self, p: dict) -> None:
        """On serverless, stop at a safe point once the request's time budget is used; everything is saved."""
        deadline = getattr(self._tl, "deadline", None)
        if deadline and time.monotonic() > deadline:
            self.st.save(p)
            raise Yield()

    # ---- plumbing ---------------------------------------------------------
    @property
    def store(self) -> Store:
        if self.s.mode not in self.stores:
            if self.s.database_url:
                st = Store(pg_url=self.s.database_url, schema=f"sprinto_{self.s.mode}")
            else:
                st = Store(self.s.db_path)
            if not self.s.serverless:
                st.x("UPDATE prospects SET busy=0")  # crash recovery: nobody holds a lease at start-up
            self.stores[self.s.mode] = st
        return self.stores[self.s.mode]

    @property
    def sim(self) -> bool:
        return self.s.mode == "simulation"

    def start_run(self, domains: list[str], label: str = "") -> int:
        rid = self.store.create_run(self.s.mode, versions(), domains, label)
        for p in self.store.q("SELECT id FROM prospects WHERE run_id=?", (rid,)):
            self.enqueue(p["id"])
        return rid

    def enqueue(self, pid: int) -> None:
        if self.s.serverless:
            return  # no background threads on serverless: the dashboard calls process_now() per company
        st = self.store
        self.pool.submit(self._safe_process, st, pid)

    def state_store(self) -> Store:
        if "__state__" not in self.stores:
            self.stores["__state__"] = Store(pg_url=self.s.database_url, schema="sprinto_state")
        return self.stores["__state__"]

    def process_now(self, pid: int) -> bool:
        """Run one company to completion (or to a pause) inside this request. Returns False if another worker has it."""
        st = self.store
        if st.prospect(pid)["outcome"]:
            return True
        self._safe_process(st, pid)
        return True

    def resume_pending(self) -> int:
        if self.s.serverless:
            return 0
        rows = self.store.q("SELECT id FROM prospects WHERE outcome='' AND busy=0")
        for r in rows:
            self.enqueue(r["id"])
        return len(rows)

    def retry(self, pid: int) -> None:
        p = self.store.prospect(pid)
        if p["outcome"] in TERMINAL and p["outcome"] not in ("NO_SUITABLE_CONTACT", "EMAIL_QA_FAILED", "PAIN_EVIDENCE_THIN", "INSUFFICIENT_EVIDENCE"):
            return
        if p["outcome"] == "NO_SUITABLE_CONTACT":
            p["stage"] = "QUALIFIED"
        elif p["outcome"] in ("EMAIL_QA_FAILED",):
            p["stage"] = "PAIN_READY"
        elif p["outcome"] == "PAIN_EVIDENCE_THIN":
            p["stage"] = "CONTACT_SELECTED"
        elif p["outcome"] == "INSUFFICIENT_EVIDENCE":
            p["data"]["extra_rounds"] = p["data"].get("extra_rounds", 0) + 1
            p["stage"] = "RESEARCHING"
        self.store.save(p, outcome="", outcome_reason="")
        self.store.log(p, p["step"], "info", "Retry requested by operator")
        self.enqueue(pid)

    def _step(self, p: dict, n: int, state: str, note: str = "") -> None:
        steps = p["data"].setdefault("steps", {})
        steps[str(n)] = {"state": state, "note": note}
        p["step"] = n
        self.store.save(p)

    def _safe_process(self, st: Store, pid: int) -> None:
        if not st.set_busy(pid, True):
            return
        p = st.prospect(pid)
        # Vercel cuts requests at ~60 s, so each serverless request works for ~25 s, then hands over.
        self._tl.deadline = time.monotonic() + SERVERLESS_BUDGET_S if self.s.serverless else None
        try:
            self._process(st, p)
        except Yield:
            st.save(p)
        except Pause as e:
            self._step(p, p["step"] or 1, "paused", e.reason)
            st.save(p, outcome=e.outcome, outcome_reason=e.reason)
            st.log(p, p["step"], "pause", e.reason, outcome=e.outcome)
        except Stop as e:
            if e.outcome not in ("SENT", "DRY_RUN_RECORDED", "SENT_SIMULATED"):
                self._step(p, p["step"] or 1, "stopped", e.reason)
            st.save(p, outcome=e.outcome, outcome_reason=e.reason)
            st.log(p, p["step"], "outcome", e.reason, outcome=e.outcome)
        except LLMError as e:
            outcome = {"auth": "NEEDS_SETUP", "quota": "WAITING_QUOTA"}.get(e.kind, "NEEDS_HUMAN")
            reason = str(e) if e.kind != "quota" else f"Gemini quota reached — retry after the reset. ({e})"
            self._step(p, p["step"] or 1, "paused", reason)
            st.save(p, outcome=outcome, outcome_reason=reason)
            st.log(p, p["step"], "pause", reason, outcome=outcome)
        except Exception as e:  # noqa: BLE001 — one company's failure never stops the batch
            reason = f"{type(e).__name__}: {e}"
            self._step(p, p["step"] or 1, "failed", reason)
            st.save(p, outcome="NEEDS_HUMAN", outcome_reason=reason)
            st.log(p, p["step"], "error", reason, trace=traceback.format_exc()[-2000:])
        finally:
            st.set_busy(pid, False)

    def _process(self, st: Store, p: dict) -> None:
        self.st = st
        first = True
        while True:
            if not first:  # always make at least one step of progress per request
                self._checkpoint(p)
            first = False
            stage = p["stage"]
            if stage in ("QUEUED", "RESEARCHING"):
                self.research(p)
            elif stage == "QUALIFIED":
                self.find_contact(p)
            elif stage == "CONTACT_SELECTED":
                self.pain_points(p)
            elif stage == "PAIN_READY":
                self.draft(p)
            elif stage == "DRAFT_READY":
                self.send(p)
            else:
                return

    # ---- steps 1-3: research & qualification loop ------------------------
    def _new_pid(self, p: dict, prefix: str) -> str:
        return f"{prefix}{len(self.st.pages(p['id'], with_text=False)) + 1}"

    def _company_pages_used(self, p: dict) -> int:
        return sum(1 for x in self.st.pages(p["id"], with_text=False) if x["source_type"] == "company_site")

    def _fetch_pages(self, p: dict, fetcher: Fetcher, targets: list[tuple[str, str, str]], round_no: int) -> list[dict]:
        st = self.st
        have = {pg["url"] for pg in st.pages(p["id"], with_text=False)}
        cap = load_config("rubric_v1")["max_pages_per_company"]
        new = []
        for url, cat, reason in targets:
            if url in have or self._company_pages_used(p) >= cap:
                continue
            pg = fetcher.fetch(url)
            rec = {"pid": self._new_pid(p, "P"), "url": pg.url, "ok": pg.ok, "status": pg.status, "method": pg.method,
                   "category": cat or categorise(pg.url) or "page", "round": round_no, "reason": reason,
                   "title": pg.title, "text": pg.text, "error": pg.error, "thin": pg.thin,
                   "extra": {"links": pg.links[:150], "ats": pg.ats}}
            st.add_page(p["id"], rec)
            have.add(pg.url)
            st.log(p, 1, "fetch", f"{'Read' if pg.ok else 'Could not read'} {pg.url}" + (f" — {pg.error}" if pg.error else
                   f" ({len(pg.text):,} chars{', thin' if pg.thin else ''})"), url=pg.url, reason=reason, round=round_no)
            if pg.ok:
                new.append(rec)
            if pg.ats:
                self._job_boards(p, pg.ats, round_no)
        return new

    def _probe_careers(self, p: dict, fetcher: Fetcher, home_url: str, round_no: int) -> None:
        """No careers link on the homepage: try the standard paths on the company's own domain (only a hit is kept)."""
        st = self.st
        for path in ("/careers", "/jobs", "/company/careers"):
            url = f"https://{p['domain']}{path}"
            pg = fetcher.fetch(url)
            if pg.ats:
                st.log(p, 1, "fetch", f"{url} redirects to the company's job board")
                self._job_boards(p, pg.ats, round_no)
                return
            if pg.ok and pg.url != home_url and not pg.thin:
                self._fetch_pages(p, fetcher, [(url, "careers", "standard careers path (not linked from homepage)")],
                                  round_no)
                return
        st.log(p, 1, "fetch", "No careers page or job board found at /careers, /jobs or /company/careers")

    def _job_boards(self, p: dict, boards: list, round_no: int) -> None:
        """#1 Read job boards (Greenhouse/Lever/Ashby/Workable) linked from the company's own pages."""
        st, d = self.st, p["data"]
        done = d.setdefault("ats_done", [])
        for provider, token in boards or []:
            key = f"{provider}:{token}"
            if key in done or len(done) >= 2:
                continue
            done.append(key)
            try:
                board = ATS.fetch_board(provider, token, self.sim)
            except Exception as e:  # noqa: BLE001 — a missing board never stops research
                st.log(p, 1, "fetch", f"Job board {key} linked from the site could not be read: {e}")
                continue
            rec = {"pid": self._new_pid(p, "J"), "url": board["url"], "ok": True, "status": 200,
                   "method": f"{provider}_api", "category": "careers", "round": round_no,
                   "reason": "job board linked from the company's own site", "title": f"{provider.title()} job board",
                   "text": board["text"], "error": "", "thin": False, "source_type": "job_board",
                   "extra": {"links": [], "relevant": board["relevant"], "jobs": len(board["jobs"])}}
            st.add_page(p["id"], rec)
            rel = board["relevant"]
            st.log(p, 1, "fetch", f"Read {provider.title()} job board ({len(board['jobs'])} open roles"
                   + (f"; security/compliance-related: {', '.join(rel[:4])}" if rel else "; none security/compliance-related")
                   + ")", url=board["url"])
        st.save(p)

    def _candidate_links(self, p: dict) -> list[str]:
        pages = self.st.pages(p["id"], with_text=False)
        have = {pg["url"] for pg in pages}
        links, seen = [], set()
        sitemap = [{"url": u} for u in p["data"].get("sitemap_urls", [])]
        for l in [l for pg in pages if pg["source_type"] == "company_site" for l in pg["extra"].get("links", [])] + sitemap:
            u = l["url"]
            if u in have or u in seen or ASSET_RE.search(u):
                continue
            seen.add(u)
            links.append(u)
        links.sort(key=lambda u: (categorise(u) is None, len(u)))
        return links[:40]

    def _sitemap_round(self, p: dict, fetcher: Fetcher, round_no: int) -> list[str]:
        """#4 Read the sitemap once and fetch the best page for each page type we haven't read yet."""
        st, d = self.st, p["data"]
        if d.get("sitemap_done"):
            return []
        d["sitemap_done"] = True
        urls, note = fetcher.sitemap_urls()
        d["sitemap_urls"] = [u for u in urls if categorise(u)][:300]
        read = {pg["category"] for pg in st.pages(p["id"], with_text=False) if pg["ok"]}
        want = [c for c in ("security", "careers", "press", "customers", "pricing", "company") if c not in read]
        have = {pg["url"] for pg in st.pages(p["id"], with_text=False)}
        picks = [(u, c) for u, c in pick_category_pages([{"url": u} for u in d["sitemap_urls"]], have, limit=6)
                 if c in want][:3]
        st.log(p, 3, "loop", f"Sitemap: {note}. Page types not read yet: {', '.join(want) or 'none'}"
               + (f" → fetching {', '.join(c for _, c in picks)}" if picks else ""))
        self._fetch_pages(p, fetcher, [(u, c, f"{c} page found in sitemap.xml") for u, c in picks], round_no)
        return [u for u, _ in picks]

    def _extract(self, p: dict, new_pages: list[dict], round_no: int) -> None:
        if not new_pages:
            return
        st, d = self.st, p["data"]
        self._step(p, 2, "running", f"Extracting facts from {len(new_pages)} page(s)")
        for pg in new_pages:
            pg.setdefault("source_type", "company_site")
            pg["links_company"] = bool(pg.get("extra", {}).get("links_company"))
        llm = get_llm(self.s)
        known = d.get("fields", {})
        prompt = EX.build_prompt(new_pages, known)
        out = llm.generate_json("extract", prompt, ExtractionOut, context={"pages": new_pages, "domain": p["domain"]})
        start = len(st.evidence(p["id"]))
        fields, evidence, rejected = EX.validate(out, new_pages, start, round_no,
                                                 company=d.get("company_name", ""), domain=p["domain"])
        st.add_evidence(p["id"], evidence)
        d["fields"] = EX.merge(known, fields)
        d.setdefault("rejected_citations", []).extend(rejected)
        d["extracted"] = sorted(set(d.get("extracted", [])) | {pg["pid"] for pg in new_pages})
        inferred = sum(1 for e in evidence if e["status"] == "inferred")
        st.log(p, 2, "extract", f"{len(evidence)} evidence excerpts verified word-for-word"
               + (f" ({inferred} from third-party sources → inferred)" if inferred else "")
               + (f", {len(rejected)} rejected" if rejected else ""), rejected=rejected[:10])
        name = d["fields"].get("company_name", {}).get("value")
        if name and d.get("company_name") in (None, "", p["domain"]):
            d["company_name"] = EX.clean_company_name(name) or p["domain"]
        self._step(p, 2, "done", f"{sum(1 for v in d['fields'].values() if v['status'] != 'unknown')}/12 fields sourced")

    def research(self, p: dict) -> None:
        st, d = self.st, p["data"]
        rubric = load_config("rubric_v1")
        fetcher = Fetcher(p["domain"], simulation=self.sim)
        p["stage"] = "RESEARCHING"
        d.setdefault("rounds", [])
        d.setdefault("company_name", p["domain"])

        # Step 1: homepage + one page per category. If nothing was read successfully (e.g. a retry after the site
        # was unreachable), start the read again instead of scoring an empty company.
        if not any(pg["ok"] for pg in st.pages(p["id"], with_text=False)):
            st.x("DELETE FROM pages WHERE prospect_id=?", (p["id"],))
            d["rounds"], d["ats_done"] = [], []
            self._step(p, 1, "running", "Reading homepage")
            home = fetcher.homepage()
            rec = {"pid": "P1", "url": home.url, "ok": home.ok, "status": home.status, "method": home.method,
                   "category": "home", "round": 0, "reason": "homepage", "title": home.title, "text": home.text,
                   "error": home.error, "thin": home.thin, "extra": {"links": home.links[:150], "ats": home.ats}}
            st.add_page(p["id"], rec)
            st.log(p, 1, "fetch", f"{'Read' if home.ok else 'Could not read'} homepage {home.url}"
                   + (f" — {home.error}" if home.error else f" ({len(home.text):,} chars)"))
            if not home.ok:
                raise Pause("NEEDS_HUMAN", f"Homepage unreachable ({home.error}). A blocked site is a retrieval failure, "
                                           "not a negative signal — check the domain and retry.")
            self._job_boards(p, home.ats, 0)
            picks = pick_category_pages(home.links, {home.url}, limit=len(CATEGORIES))
            self._step(p, 1, "running", f"Reading {len(picks)} key pages")
            self._fetch_pages(p, fetcher, [(u, c, f"best {c} page linked from homepage") for u, c in picks], 0)
            if "careers" not in {c for _, c in picks} and not d.get("ats_done"):
                self._probe_careers(p, fetcher, home.url, 0)
            d["rounds"].append({"round": 0, "pages": [u for u, _ in picks], "search": "", "gaps": []})
        ok_pages = [pg for pg in st.pages(p["id"]) if pg["ok"] and pg["source_type"] != "search_lead"]
        self._step(p, 1, "done", f"{len(ok_pages)} pages read")

        max_rounds = rubric["max_research_rounds"] + d.get("extra_rounds", 0)
        first_round = True
        while True:
            if not first_round:
                self._checkpoint(p)
            first_round = False
            round_no = len(d["rounds"]) - 1
            todo = [pg for pg in st.pages(p["id"]) if pg["ok"] and pg["source_type"] != "search_lead"
                    and pg["pid"] not in d.get("extracted", [])]
            self._extract(p, todo, round_no)

            # Step 3: qualify
            self._step(p, 3, "running", f"Scoring against rubric (round {round_no + 1})")
            evidence = st.evidence(p["id"])
            links = self._candidate_links(p)
            out = get_llm(self.s).generate_json(
                "qualify", Q.build_prompt(p["domain"], d["fields"], evidence, links, date.today().isoformat()), QualOut,
                context={"evidence": evidence, "fields": d["fields"], "links": links, "round": round_no, "domain": p["domain"]})
            result = Q.score(out, d["fields"], evidence, p["domain"], date.today())
            d["qualification"] = result
            st.log(p, 3, "qualify", f"Round {round_no + 1}: score {result['total']:g}/100 (best case {result['best_case']:g}) → "
                   f"{result['decision'].replace('_', ' ').lower()}", gaps=out.gaps)
            self.st.save(p)

            if result.get("needs_funding_check") and not d.get("funding_checked"):
                d["funding_checked"] = True
                stage = result["stage"]
                gap = (f"Only {stage['label']} is stated ({stage['eid']}, {stage['source_type'].replace('_', ' ')}) — "
                       f"checking the newsroom and credible press for a later round before ruling out")
                query = (f"What is the latest funding round raised by {d.get('company_name') or p['domain']} "
                         f"({p['domain']}), which series, and when?")
                st.log(p, 3, "loop", gap)
                self._step(p, 3, "running", "Verifying latest funding round")
                pages = self._sitemap_round(p, fetcher, round_no + 1)
                self._search(p, query, round_no + 1)
                d["rounds"].append({"round": round_no + 1, "pages": pages, "search": query, "gaps": [gap]})
                st.save(p)
                continue

            # A qualified company with no timely signal still deserves one cheap look for its careers/press pages:
            # "why now" makes the email's hook, even when it doesn't change the decision.
            rn = next((x for x in result["dimensions"] if x["key"] == "reason_now"), None)
            if result["decision"] == "QUALIFIED" and rn and rn["level"] == 0 and not d.get("sitemap_done") \
                    and not d.get("why_now_checked"):
                d["why_now_checked"] = True
                read = {pg["category"] for pg in st.pages(p["id"], with_text=False) if pg["ok"]}
                if not {"careers", "press"} <= read:
                    gap = "Qualified, but no timely signal yet — looking for careers / newsroom pages for a hook"
                    st.log(p, 3, "loop", gap)
                    self._step(p, 3, "running", "Looking for a timely signal (hiring / news)")
                    pages = self._sitemap_round(p, fetcher, round_no + 1)
                    if pages:
                        d["rounds"].append({"round": round_no + 1, "pages": pages, "search": "", "gaps": [gap]})
                        st.save(p)
                        continue

            if result["decision"] != "UNDECIDED":
                break
            if len(d["rounds"]) - 1 >= max_rounds:  # extra rounds beyond the initial read are capped
                raise Stop("INSUFFICIENT_EVIDENCE",
                           f"Still undecided after {len(d['rounds'])} research rounds (score {result['total']:g}, best case "
                           f"{result['best_case']:g}). Gaps: {'; '.join(out.gaps[:3]) or 'n/a'}")

            # Agent loop: the LLM names the gap, code limits what it can fetch. First round also reads the sitemap.
            self._step(p, 3, "running", f"Undecided — researching gaps: {'; '.join(out.gaps[:2]) or 'more context'}")
            sm_pages = self._sitemap_round(p, fetcher, round_no + 1)
            links = self._candidate_links(p)
            allowed = set(links)
            nxt = [u for u in out.next_pages if u in allowed][: rubric["max_new_pages_per_round"]]
            new_round = {"round": round_no + 1, "pages": sm_pages + nxt, "search": "", "gaps": out.gaps}
            st.log(p, 3, "loop", f"Undecided. Gaps: {'; '.join(out.gaps[:3]) or 'n/a'}. Fetching {len(nxt)} page(s)"
                   + (f" + web search “{out.search_query}”" if out.search_query else ""),
                   proposed_pages=out.next_pages, accepted_pages=nxt)
            self._fetch_pages(p, fetcher, [(u, categorise(u) or "page", f"gap: {(out.gaps or ['more context'])[0]}") for u in nxt],
                              round_no + 1)
            if out.search_query:
                new_round["search"] = out.search_query
                self._search(p, out.search_query, round_no + 1)
            d["rounds"].append(new_round)
            if not nxt and not out.search_query and not sm_pages:
                raise Stop("INSUFFICIENT_EVIDENCE", f"Undecided and no further pages or searches could fill the gaps: "
                                                    f"{'; '.join(out.gaps[:3]) or 'n/a'}")

        decision = result["decision"]
        top = sorted([x for x in result["dimensions"] if x["level"] > 0], key=lambda x: -x["points"])
        d["trail"] = d.get("trail", {})
        d["trail"]["company"] = {"decision": decision, "why": result["why"], "stage_note": result["stage_note"],
                                 "drivers": [{"label": x["label"], "points": x["points"], "weight": x["weight"],
                                              "reason": x["reason"], "evidence_ids": x["evidence_ids"]} for x in top[:4]]}
        self._step(p, 3, "done", f"{decision.replace('_', ' ').title()} — {result['total']:g}/100")
        if decision != "QUALIFIED":
            raise Stop(decision, result["why"])
        p["stage"] = "QUALIFIED"
        self.st.save(p)

    def _search(self, p: dict, query: str, round_no: int) -> None:
        """#2/#3 Web search results are LEADS: fetch the real article behind each credible source and let the
        validator quote it word for word. Google's summary itself is never evidence."""
        st = self.st
        try:
            res = get_llm(self.s).grounded_search(query, context={"domain": p["domain"]})
        except LLMError as e:
            st.log(p, 3, "search", f"Web search skipped: {e}")
            return
        lead = {"pid": self._new_pid(p, "S"), "url": "https://www.google.com/search", "ok": True, "status": 200,
                "method": "gemini_google_search", "category": "search", "round": round_no, "reason": f"search: {query}",
                "title": f"Search: {query}", "text": "\n".join(s["text"] for s in res["statements"]) or res["text"],
                "error": "", "thin": False, "source_type": "search_lead",
                "extra": {"sources": res["sources"], "statements": res["statements"], "links": []}}
        st.add_page(p["id"], lead)
        cap = load_config("sources_v1").get("max_articles_per_search", 3)
        fetched, notes = 0, []
        for src in res["sources"]:
            if fetched >= cap:
                break
            hint = SRC.host_of(src.get("title") or "") if "." in (src.get("title") or "") else ""
            if hint and (SRC.never_fetch("https://" + hint) or SRC.tier("https://" + hint, p["domain"], self.sim) == "lead"):
                notes.append(f"{hint}: not a credible source (lead only)")
                continue
            url = resolve_redirect(src.get("uri", ""), self.sim)
            if not url or SRC.never_fetch(url):
                notes.append(f"{hint or src.get('uri', '')[:40]}: could not resolve")
                continue
            t = SRC.tier(url, p["domain"], self.sim)
            if t == "lead":
                notes.append(f"{SRC.host_of(url)}: not a credible source (lead only)")
                continue
            host = SRC.host_of(url)
            page = Fetcher(host if t != "1" else p["domain"], simulation=self.sim).fetch(url)
            fetched += 1
            links_company = any(h == p["domain"] or h.endswith("." + p["domain"]) for h in page.ext_hosts) \
                or p["domain"] in page.text.lower()
            rec = {"pid": self._new_pid(p, "A" if t != "1" else "P"), "url": page.url, "ok": page.ok,
                   "status": page.status, "method": page.method, "category": "press", "round": round_no,
                   "reason": f"source of web search “{query[:60]}”", "title": page.title, "text": page.text,
                   "error": page.error, "thin": page.thin, "source_type": SRC.TYPE_FOR_TIER[t],
                   "extra": {"links": [], "tier": t, "links_company": links_company}}
            st.add_page(p["id"], rec)
            notes.append(f"{host}: {'read' if page.ok else 'could not read — ' + page.error} "
                         f"(tier {t}{', links to ' + p['domain'] if page.ok and links_company else ''})")
        st.log(p, 3, "search", f"Web search “{query}” → {len(res['sources'])} source(s); fetched {fetched} real article(s) "
               f"to quote from. " + "; ".join(notes[:5]), sources=res["sources"][:5])
        st.save(p)

    # ---- step 4: contact ------------------------------------------------------
    def find_contact(self, p: dict) -> None:
        st, d = self.st, p["data"]
        self._step(p, 4, "running", "Searching Hunter for decision-makers")
        raw = st.hunter_cached(p["domain"])
        hunter_note = ""
        if raw is None:
            try:
                raw = C.hunter_search(p["domain"], self.s.hunter_key, self.sim)
                st.hunter_put(p["domain"], raw)
                st.log(p, 4, "hunter", f"Hunter returned {len(raw.get('data', {}).get('emails', []))} people")
            except C.HunterError as e:
                raw, hunter_note = {"data": {"emails": []}}, str(e)
                st.log(p, 4, "hunter", f"Hunter unavailable: {e}")
                if not d.get("manual_contacts"):
                    raise Pause("NEEDS_SETUP" if e.kind == "auth" else "NEEDS_HUMAN",
                                f"{e}. Add a contact manually (e.g. from Apollo) and retry.")
        else:
            st.log(p, 4, "hunter", "Used cached Hunter result (saves quota)")
        people = list(raw.get("data", {}).get("emails", []))
        people += [{**m, "source": "manual"} for m in d.get("manual_contacts", [])]
        cands = sorted((C.score_candidate(c, p["domain"]) for c in people), key=lambda c: -c["total"])
        chosen, why = C.select(cands)
        d["candidates"] = cands
        d["hunter_note"] = hunter_note
        if not chosen:
            self._step(p, 4, "done", "No suitable contact")
            raise Stop("NO_SUITABLE_CONTACT", f"{why} ({len(cands)} candidate(s) checked). Add an Apollo contact to continue.")
        d["contact"] = chosen
        d.setdefault("trail", {})["person"] = {"why": why, "name": chosen["name"], "title": chosen["title"],
                                               "source": chosen["source"], "scores": chosen["scores"], "total": chosen["total"]}
        st.log(p, 4, "contact", f"Selected {chosen['name']} — {chosen['title']} ({chosen['total']}/10)")
        self._step(p, 4, "done", f"{chosen['name']}, {chosen['title']}")
        p["stage"] = "CONTACT_SELECTED"
        st.save(p)

    # ---- step 5: pains ------------------------------------------------------
    def pain_points(self, p: dict) -> None:
        st, d = self.st, p["data"]
        evidence = st.evidence(p["id"])
        company = d.get("company_name") or p["domain"]
        feedback = ""
        for attempt in range(2):
            self._step(p, 5, "running", "Forming pain hypotheses" + (" (retry)" if attempt else ""))
            out = get_llm(self.s).generate_json(
                "pains", PN.build_prompt(company, d["contact"], d["fields"], evidence, feedback), PainsOut,
                context={"evidence": evidence, "contact": d["contact"], "fields": d["fields"], "company": company})
            res = PN.score(out, evidence, d["contact"])
            d["pains"] = res
            st.log(p, 5, "pains", f"{len(res['all'])} proposed, {len([h for h in res['all'] if h['passed']])} passed (≥7/10)")
            if res["ok"]:
                break
            feedback = "; ".join(f"'{h['title']}' scored {h['total']}/10 ({h['scores']})" for h in res["all"]) + \
                       ". Cite at least two evidence ids each, use hedged wording, name a consequence, and keep them distinct."
        if not res["ok"]:
            self._step(p, 5, "done", "Evidence too thin")
            raise Stop("PAIN_EVIDENCE_THIN", "Could not form two distinct, evidence-backed pain hypotheses (≥7/10 each).")
        d.setdefault("trail", {})["pains"] = [{"title": h["title"], "statement": h["statement"], "total": h["total"],
                                               "evidence_ids": h["evidence_ids"]} for h in res["chosen"]]
        self._step(p, 5, "done", " · ".join(h["title"] for h in res["chosen"]))
        p["stage"] = "PAIN_READY"
        st.save(p)

    # ---- step 6: email ------------------------------------------------------
    def draft(self, p: dict) -> None:
        st, d = self.st, p["data"]
        pol = load_config("policy_v1")
        evidence = st.evidence(p["id"])
        company = d.get("company_name") or p["domain"]
        contact, pains = d["contact"], d["pains"]["chosen"]
        attempts, feedback = [], ""
        for i in range(1 + pol["email_max_revisions"]):
            if i:
                self._checkpoint(p)
            self._step(p, 6, "running", "Writing email" if i == 0 else f"Revising email ({i}/{pol['email_max_revisions']})")
            parts = get_llm(self.s).generate_json(
                "email", EG.build_prompt(company, contact, pains, evidence, feedback), EmailParts,
                context={"company": company, "contact": contact, "pains": pains, "evidence": evidence, "attempt": i})
            body = EG.assemble(parts, self.s.sender_name)
            ev = get_llm(self.s).generate_json("evaluate_email", EG.build_eval_prompt(body, contact, company), EmailEval,
                                               context={"body": body})
            ev_total = ev.hook + ev.hypothesis + ev.value + ev.next_step + ev.clarity
            chk = EG.checks(parts, body, contact, company, evidence)
            ok = all(c["ok"] for c in chk) and ev_total >= pol["email_eval_pass"]
            attempts.append({"parts": parts.model_dump(), "body": body, "eval": {**ev.model_dump(), "total": ev_total},
                             "checks": chk, "passed": ok})
            st.log(p, 6, "email", f"Draft {i + 1}: evaluator {ev_total}/10, {sum(c['ok'] for c in chk)}/{len(chk)} checks"
                   + (" — passed" if ok else ""))
            if ok:
                break
            feedback = "; ".join(f"{c['label']}: {c['detail']}" for c in chk if not c["ok"])
            if ev_total < pol["email_eval_pass"]:
                feedback += f"; evaluator {ev_total}/10: {ev.notes}"
        d["email"] = {"attempts": attempts, "final": attempts[-1]}
        if not attempts[-1]["passed"]:
            self._step(p, 6, "done", "QA failed")
            raise Stop("EMAIL_QA_FAILED", f"Email failed QA after {len(attempts)} attempts: {feedback[:300]}")
        fin = attempts[-1]
        hook_ev = [e for e in evidence if e["eid"] in fin["parts"]["opening_evidence_ids"]]
        d.setdefault("trail", {})["email"] = {
            "hook": fin["parts"]["opening_fact"], "hook_evidence": hook_ev,
            "pains_used": [h["title"] for h in pains], "eval": fin["eval"],
            "checks_passed": sum(c["ok"] for c in fin["checks"]), "checks_total": len(fin["checks"]), "revisions": len(attempts) - 1}
        self._step(p, 6, "done", f"Passed QA ({fin['eval']['total']}/10, {len(fin['checks'])}/{len(fin['checks'])} checks)")
        p["stage"] = "DRAFT_READY"
        st.save(p)

    # ---- step 7: send -------------------------------------------------------
    def send(self, p: dict) -> None:
        st, d = self.st, p["data"]
        op_id = f"outreach:{p['run_id']}:{p['domain']}"
        prior = st.send_op(op_id)
        outcome_for = {"sent": "SENT", "dry_run": "DRY_RUN_RECORDED", "simulated": "SENT_SIMULATED"}
        if prior and prior["status"] in outcome_for:  # never re-execute an irreversible action
            st.log(p, 7, "send", f"Already {prior['status']} — not sending again (op {op_id})")
            raise Stop(outcome_for[prior["status"]], f"Already {prior['status']} to {prior['to_addr']}")
        if prior and prior["status"] in ("pending", "uncertain"):
            raise Pause("SEND_UNCERTAIN", "A previous send attempt may have gone through. Check your Gmail Sent folder, "
                                          "then mark it sent or not sent.")
        fin = d["email"]["final"]
        contact = d["contact"]
        company = d.get("company_name") or p["domain"]
        self._step(p, 7, "running", f"Sending to {self.s.delivery_address or '(not set)'}")
        st.put_send(op_id, p["id"], status="pending", mode=self.s.mode, to=self.s.delivery_address)
        try:
            res = mailer.send(self.s, company, fin["parts"]["subject"], fin["body"],
                              f"{contact['name']}, {contact['title']} at {company}")
        except mailer.SetupRequired as e:
            st.put_send(op_id, p["id"], status="not_sent", mode=self.s.mode, error=str(e))
            raise Pause("NEEDS_SETUP", str(e))
        except mailer.SendUncertain as e:
            st.put_send(op_id, p["id"], status="uncertain", mode=self.s.mode, error=str(e))
            raise Pause("SEND_UNCERTAIN", str(e))
        except (ConnectionError, mailer.DeliveryPolicyViolation) as e:
            st.put_send(op_id, p["id"], status="failed", mode=self.s.mode, error=str(e))
            raise Pause("NEEDS_HUMAN", f"Send failed before Gmail accepted it: {e}")
        st.put_send(op_id, p["id"], status=res["status"], mode=self.s.mode, message_id=res["message_id"],
                    to=res["to"], subject=res["subject"], body=res["body"])
        d["send"] = {**res, "op_id": op_id}
        label = {"sent": f"Sent to {res['to']}", "dry_run": f"Dry run — recorded, not sent (would go to {res['to']})",
                 "simulated": f"Simulated send to {res['to']}"}[res["status"]]
        st.log(p, 7, "send", label, message_id=res["message_id"])
        self._step(p, 7, "done", label)
        p["stage"] = "DONE"
        raise Stop(outcome_for[res["status"]], label)

    def resolve_uncertain(self, pid: int, was_sent: bool) -> None:
        p = self.store.prospect(pid)
        op_id = f"outreach:{p['run_id']}:{p['domain']}"
        self.store.put_send(op_id, pid, status="sent" if was_sent else "not_sent", mode=self.s.mode,
                            to=self.s.delivery_address)
        self.store.log(p, 7, "send", f"Operator confirmed the send was {'delivered' if was_sent else 'NOT delivered'}")
        if was_sent:
            self.store.save(p, stage="DONE", outcome="SENT", outcome_reason="Confirmed by operator from Gmail Sent folder")
        else:
            self.st = self.store
            self.store.x("DELETE FROM sends WHERE op_id=?", (op_id,))
            self.store.save(p, stage="DRAFT_READY", outcome="", outcome_reason="")
            self.enqueue(pid)
