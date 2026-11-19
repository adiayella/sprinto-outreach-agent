from sprinto_outreach.email_gen import repetitions
from sprinto_outreach.qualification import current_stage

MOMENTIC = """Hi Jeff,

Noticed Momentic holds a SOC 2 Type 2 certification and lists enterprise engineering teams like Notion, Retool, and Webflow as customers. With enterprise customers like Notion and Retool, my guess is your team might be spending significant cycles handling security questionnaires and keeping your Trust Center up to date.

Sprinto helps by keeping a trust centre and answers to security questionnaires current for enterprise deals.

Is streamlining enterprise security reviews on your radar this quarter?

Best,
Growth team, Sprinto"""

CAL = """Hi Keith,

Cal.com maintains certifications across ISO 27001, SOC 2 Type II, HIPAA, GDPR, and CCPA. My guess is that managing overlapping controls across all these frameworks might take up valuable engineering time, especially as product architecture shifts with releases like v7.

Sprinto helps by mapping one set of controls across SOC 2, ISO 27001, GDPR, and HIPAA so your engineering team does not have to repeat compliance work.

Is streamlining cross-framework maintenance something on your radar this quarter?

Best,
Growth team, Sprinto"""


def ev(eid, text, src="company_site"):
    return {"eid": eid, "field": "funding_stage", "excerpt": text, "source_type": src}


def test_momentic_repetition_caught():
    reps = repetitions(MOMENTIC, "Momentic", {"first_name": "Jeff"})
    assert any("Notion" in r for r in reps) and any("Retool" in r for r in reps), reps


def test_cal_email_clean():
    assert repetitions(CAL, "Cal.com", {"first_name": "Keith"}) == []


def test_latest_stage_wins():
    s = current_stage([ev("E1", "Nov $4M seed round OSS Capital backed our next phase"),
                       ev("S1", "Plane raised a $30M Series A in 2025.", "web_search")])
    assert s["label"] == "Series A" and s["eid"] == "S1"


def test_pre_seed_is_not_seed():
    assert current_stage([ev("E1", "We closed our pre-seed round")])["label"] == "Pre-seed"
    assert current_stage([]) is None


def test_apex_to_www_redirect_does_not_loop(monkeypatch):
    import httpx
    from sprinto_outreach import fetcher as F

    def handler(req):
        if req.url.host == "acme.com":
            return httpx.Response(301, headers={"location": f"https://www.acme.com{req.url.path}"})
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, headers={"content-type": "text/html"},
                              html="<html><body><main><p>" + "Acme is a SaaS platform. " * 30 + "</p></main></body></html>")

    real = httpx.Client
    monkeypatch.setattr(F.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(F, "check_url", lambda url, resolve=True: None)
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    page = F.Fetcher("acme.com").homepage()
    assert page.ok, page.error
    assert "SaaS platform" in page.text


# ---- search improvements (#1-#5) ------------------------------------------------
def test_find_boards_in_raw_html():
    from sprinto_outreach.ats import find_boards
    html = ('<a href="https://jobs.ashbyhq.com/momentic">Jobs</a>'
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
            '<iframe src="https://jobs.lever.co/beta-co"></iframe>')
    assert find_boards(html) == [("greenhouse", "acme"), ("lever", "beta-co"), ("ashby", "momentic")]


def test_source_tiers():
    from sprinto_outreach.sources import never_fetch, tier
    assert tier("https://acme.com/about", "acme.com") == "1"
    assert tier("https://techcrunch.com/2026/acme-raises", "acme.com") == "2"
    assert tier("https://www.tracxn.com/d/acme", "acme.com") == "3"
    assert tier("https://randomblog.io/acme", "acme.com") == "lead"
    assert never_fetch("https://www.youtube.com/watch?v=1") and never_fetch("https://linkedin.com/company/acme")


def test_customer_compliance_rejected():
    from sprinto_outreach.extraction import is_customer_compliance
    assert is_customer_compliance("Our release logs help engineering teams get SOC 2 ready faster.") is False  # "our" = own framing
    assert is_customer_compliance("Acme helps startups get SOC 2 compliant in weeks.")
    assert not is_customer_compliance("We completed our SOC 2 Type II audit in 2026.")
    assert not is_customer_compliance("Certified for ISO 27001, SOC 2 Type II, HIPAA, GDPR and CCPA.")


def _prospect(pipe, domain):
    rid = pipe.start_run([domain])
    import time
    for _ in range(100):
        p = pipe.store.q("SELECT id, outcome FROM prospects WHERE run_id=?", (rid,))[0]
        if p["outcome"]:
            break
        time.sleep(0.1)
    return pipe.store.prospect(p["id"]), pipe.store.pages(p["id"], with_text=False), pipe.store.evidence(p["id"])


def test_job_board_drives_reason_now(pipe):
    p, pages, ev = _prospect(pipe, "ledgerloop.example")
    board = [pg for pg in pages if pg["source_type"] == "job_board"]
    assert board and board[0]["url"] == "https://jobs.ashbyhq.com/ledgerloop"
    assert any(e["source_type"] == "job_board" and "GRC Analyst" in e["excerpt"] for e in ev)


def test_search_fetches_article_and_skips_leads(pipe):
    p, pages, ev = _prospect(pipe, "medibook.example")
    types = {pg["source_type"] for pg in pages}
    assert "search_lead" in types and "publication" in types           # article fetched, summary kept as a lead
    assert not any(pg["url"].startswith("https://youtube.com") for pg in pages)  # never_fetch source skipped
    art = [e for e in ev if e["source_type"] == "publication"]
    assert art and all("medibook" in e["excerpt"].lower() for e in art)  # BookWell sentence can't count
    assert not any(e["source_type"] == "search_lead" for e in ev)
    assert p["data"].get("sitemap_done")


def test_customer_framed_quote_rejected_in_pipeline(pipe):
    # "help engineering teams get SOC 2" is customer framing; with no own-framing word it must be rejected
    from sprinto_outreach.extraction import is_customer_compliance
    assert is_customer_compliance("The tool helps engineering teams get SOC 2 ready faster.")


def test_categorise_whole_words_and_blogs():
    from sprinto_outreach.fetcher import categorise
    assert categorise("https://momentic.ai/blog/cypress-alternatives") is None          # "cypress" is not "press"
    assert categorise("https://trunk.io/blog/analyzing-ci-jobs") is None                # blog about CI jobs != careers
    assert categorise("https://trunk.io/blog/trunk-raises-25m") == "press"
    assert categorise("https://acme.com/company/press") == "press"
    assert categorise("https://trust.acme.com/") == "security"
    assert categorise("https://acme.com/vision") is None                                # "iso" inside a word


def test_company_name_cleanup_and_person_check():
    from sprinto_outreach.extraction import clean_company_name
    from sprinto_outreach.email_gen import checks
    from sprinto_outreach.schemas import EmailParts
    assert clean_company_name("Cal.com, Inc.") == "Cal.com"
    assert clean_company_name("Acme Ltd") == "Acme" and clean_company_name("Momentic") == "Momentic"
    parts = EmailParts(subject="audit prep", greeting="Hi Keith,", opening_fact="Cal.com is SOC 2 certified.",
                       opening_evidence_ids=["E1"], paragraphs=["x"], question="Is it on your radar?")
    res = {c["key"]: c for c in checks(parts, "Hi Keith,\n\nCal.com is SOC 2 certified. x\n\nIs it on your radar?",
                                       {"first_name": "Keith", "name": "Keith W"}, "Cal.com, Inc.",
                                       [{"eid": "E1", "excerpt": "SOC 2"}])}
    assert res["person"]["ok"], res["person"]


def test_redirect_to_job_board_is_kept(monkeypatch):
    """acme.com/jobs -> jobs.lever.co/acme (Trunk does this): the company's own link to its board, not a refusal."""
    import httpx
    from sprinto_outreach import fetcher as F

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(302, headers={"location": "https://jobs.lever.co/acme/"})

    real = httpx.Client
    monkeypatch.setattr(F.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(F, "check_url", lambda url, resolve=True: None)
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    page = F.Fetcher("acme.com").fetch("https://acme.com/jobs")
    assert page.ats == [("lever", "acme")] and not page.ok
