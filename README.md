# Sprinto Outreach Agent

MESA Term 2 · AI Case 2 — *Cold Outreach, at Scale*. Give it a list of domains; for each company it runs the 7-step
pipeline end to end with no decisions from you, and shows *why* at every step.

| # | Step | How | Decided by |
|---|---|---|---|
| 1 | Scrape | Own scraper: homepage, then one page per category (security/trust → pricing → customers → careers → about). robots.txt, SSRF guard, 2 MB / 10 pages cap, nav/footer stripped | code |
| 2 | Extract | Gemini fills 12 fixed fields with **exact excerpts**; code keeps a fact only if the excerpt is found word for word in the page | LLM proposes, code validates |
| 3 | Qualify + agent loop | Gemini proposes rubric levels citing evidence ids; **code** scores (5 dimensions, threshold 65). If undecided, Gemini names the gaps and picks ≤2 links actually found + 1 Google-grounded search; ≤3 rounds | code |
| 4 | Point of contact | Hunter Domain Search (cached), each person scored 0–10 on 5 criteria; preference Compliance → Security → CTO → Founder. Manual Apollo entry if nobody passes | code |
| 5 | Two pain points | Gemini proposes ≤4; code re-scores (evidence, relevance, consequence, capability, honest wording) and keeps two distinct ≥7/10 | code |
| 6 | Email | Gemini writes parts; code assembles the layout; separate evaluator call (≥8/10) + 8 code checks; ≤2 revisions | code |
| 7 | Send | Gmail SMTP. `send()` has no recipient parameter: mail only ever goes to `DEMO_RECIPIENT_EMAIL`. Idempotent per run+domain | code |

ICP definition (config/rubric_v1.json): B2B SaaS handling customer data (fit), enterprise/regulated exposure, a compliance
workload, a dated reason to act now, and delivery complexity. Funding outside Series A–C → low priority; competitors → disqualified.
Absence is never evidence: no SOC 2 badge ≠ no SOC 2, a blocked page ≠ a negative signal.

## Run

```bash
cd sprinto-outreach-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python run_sprinto.py          # http://127.0.0.1:8765
.venv/bin/python -m pytest -q sprinto_outreach/tests
```

Modes (switch in the sidebar):
- **Simulate** — 5 fake companies on `.example` domains + a rule-based stand-in for Gemini. No network, no keys.
  Click *Load demo companies* → *Start run*. Expected: 2 sent, 1 needs a contact (add one → it continues), 1 low priority, 1 disqualified.
- **Dry run** — real websites, Gemini and Hunter; the email is recorded, not sent.
- **Live** — also sends, but only with `REAL_SENDS_ENABLED=true`, and only to your own inbox.

Keys go in `.env` at the repo root (see `.env.example`). Free-tier Gemini is handled: models are ranked, any failure falls
back to the next model, 429s put that model on hold until Google's stated reset, and the company pauses as
*Waiting for quota* instead of failing.

## Defending a decision

Open any company → **Decision trail**: why this company (top rubric drivers with hover-able evidence quotes), why this
person (scores + preference order), why this email (the hook's source, the pains used, evaluator + checks).
*Research & rubric* shows every page read, each research round and its gaps, and the citations the validator rejected.

Not built yet (in the project guide): reply reading + calendar scheduling (LangGraph), discovery by filters, Playwright fallback for JS-only pages.

## Deploy on Vercel

Vercel runs the app as serverless functions, so two things differ from running locally:

- **Storage** — set `DATABASE_URL` to a Postgres URL (e.g. Neon). Each mode gets its own schema
  (`sprinto_simulation`, `sprinto_dry_run`, `sprinto_real_demo`). Locally, without it, SQLite files are used.
- **Processing** — there are no background workers, so the open dashboard processes queued companies itself,
  2 at a time, one company per request (up to 300 s each, set in `vercel.json`). Close the tab and the run pauses;
  reopen it and it carries on. Leases expire after 6 minutes, so a timed-out request never blocks a company.
- **Password** — set `APP_PASSWORD`. The dashboard asks for it (any username). Without it, a Vercel deployment
  refuses every request, so nobody else can spend your API quotas.

Environment variables to set on Vercel: `DATABASE_URL`, `APP_PASSWORD`, `GEMINI_API_KEY`, `HUNTER_API_KEY`,
`RESEND_API_KEY`, `DEMO_RECIPIENT_EMAIL`, `SPRINTO_MODE`, `REAL_SENDS_ENABLED`, `SENDER_NAME`.
