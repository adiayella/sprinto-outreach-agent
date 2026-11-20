"""REST API + dashboard."""
from __future__ import annotations

import base64
import hmac
import json
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .llm import get_llm
from .pipeline import PAUSED, STEPS, TERMINAL, Pipeline
from .settings import MODES, PKG, load_config, load_settings, versions
from .urlguard import normalise_domain

settings = load_settings()
pipe = Pipeline(settings)
app = FastAPI(title="Sprinto Outreach Agent")
_DEFAULT_MODE = settings.mode


@app.middleware("http")
async def gate(request: Request, call_next):
    """Password gate (HTTP Basic, any username) whenever APP_PASSWORD is set. On a public deployment without a
    password every request is refused, so nobody else can spend your API quotas or trigger sends."""
    if settings.app_password:  # optional: set APP_PASSWORD to require a login
        ok = False
        header = request.headers.get("authorization", "")
        if settings.app_password and header.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = hmac.compare_digest(pw, settings.app_password)
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return PlainTextResponse("Password required", 401,
                                     headers={"WWW-Authenticate": 'Basic realm="Sprinto Outreach Agent"'})
    if settings.database_url:  # serverless instances share the selected mode through the database
        try:
            settings.mode = pipe.state_store().get_state("mode") or _DEFAULT_MODE
        except Exception:  # noqa: BLE001 — never block the page on this
            pass
    return await call_next(request)


@app.on_event("startup")
def _resume() -> None:
    pipe.resume_pending()


@app.get("/")
def index():
    return FileResponse(PKG / "web" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    llm = get_llm(settings)
    try:
        llm_status = llm.status()
    except Exception as e:  # noqa: BLE001
        llm_status = {"chain": [], "error": str(e)}
    return {"readiness": settings.readiness(), "llm": llm_status, "versions": versions(), "modes": list(MODES),
            "steps": STEPS, "real_sends_enabled": settings.real_sends_enabled, "sender": settings.sender_name,
            "serverless": settings.serverless}


class ModeIn(BaseModel):
    mode: str


@app.post("/api/mode")
def set_mode(body: ModeIn):
    if body.mode not in MODES:
        raise HTTPException(400, "unknown mode")
    settings.mode = body.mode
    if settings.database_url:
        pipe.state_store().set_state("mode", body.mode)
    pipe.resume_pending()
    return status()


class RunIn(BaseModel):
    domains: str
    label: str = ""


@app.post("/api/runs")
def create_run(body: RunIn):
    raw = re.split(r"[\s,;]+", body.domains)
    domains, bad = [], []
    for r in raw:
        if not r.strip():
            continue
        d = normalise_domain(r)
        (domains if d else bad).append(d or r)
    domains = list(dict.fromkeys(domains))[:25]
    if not domains:
        raise HTTPException(400, f"no valid domains{(': ' + ', '.join(bad)) if bad else ''}")
    rid = pipe.start_run(domains, body.label)
    return {"run_id": rid, "domains": domains, "rejected": bad}


def _summary(p: dict) -> dict:
    d = json.loads(p["data"]) if isinstance(p["data"], str) else p["data"]
    q = d.get("qualification") or {}
    c = d.get("contact") or {}
    return {
        "id": p["id"], "run_id": p["run_id"], "domain": p["domain"], "company": d.get("company_name") or p["domain"],
        "stage": p["stage"], "step": p["step"], "steps": d.get("steps", {}), "outcome": p["outcome"],
        "outcome_reason": p["outcome_reason"], "busy": bool(p["busy"]),
        "score": q.get("total"), "decision": q.get("decision"),
        "contact": {"name": c.get("name"), "title": c.get("title")} if c else None,
        "subject": ((d.get("email") or {}).get("final") or {}).get("parts", {}).get("subject"),
        "updated_at": p["updated_at"],
        "terminal": p["outcome"] in TERMINAL, "paused": p["outcome"] in PAUSED,
    }


@app.get("/api/runs")
def runs():
    st = pipe.store
    out = []
    for r in st.q("SELECT * FROM runs ORDER BY id DESC LIMIT 50"):
        ps = st.q("SELECT outcome, busy FROM prospects WHERE run_id=?", (r["id"],))
        out.append({**r, "versions": json.loads(r["versions"]), "total": len(ps),
                    "done": sum(1 for p in ps if p["outcome"]),
                    "sent": sum(1 for p in ps if p["outcome"] in ("SENT", "DRY_RUN_RECORDED", "SENT_SIMULATED")),
                    "qualified": 0})
    return out


@app.get("/api/runs/{rid}")
def run(rid: int):
    st = pipe.store
    r = st.q("SELECT * FROM runs WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404)
    ps = [_summary(p) for p in st.q("SELECT * FROM prospects WHERE run_id=? ORDER BY id", (rid,))]
    return {**r[0], "versions": json.loads(r[0]["versions"]), "prospects": ps}


@app.get("/api/prospects/{pid}")
def prospect(pid: int):
    st = pipe.store
    try:
        p = st.prospect(pid)
    except KeyError:
        raise HTTPException(404)
    pages = st.pages(pid, with_text=False)
    for pg in pages:
        pg["links_count"] = len(pg["extra"].pop("links", []))
    op = st.send_op(f"outreach:{p['run_id']}:{p['domain']}")
    return {"summary": _summary({**p, "data": p["data"]}), "data": p["data"], "pages": pages,
            "evidence": st.evidence(pid),
            "activity": st.q("SELECT ts, step, kind, message, detail FROM activity WHERE prospect_id=? ORDER BY id", (pid,)),
            "send": op}


@app.get("/api/pages/{page_id}")
def page_text(page_id: int):
    rows = pipe.store.q("SELECT url, title, text, source_type, extra FROM pages WHERE id=?", (page_id,))
    if not rows:
        raise HTTPException(404)
    r = rows[0]
    r["extra"] = json.loads(r["extra"] or "{}")
    r["extra"].pop("links", None)
    return r


@app.post("/api/prospects/{pid}/process")
def process(pid: int):
    """Serverless: run one company inside this request (the dashboard calls this for each queued company)."""
    try:
        pipe.process_now(pid)
    except KeyError:
        raise HTTPException(404)
    p = pipe.store.prospect(pid)
    return _summary({**p, "data": p["data"]})


@app.post("/api/prospects/{pid}/retry")
def retry(pid: int):
    pipe.retry(pid)
    return {"ok": True}


class ContactIn(BaseModel):
    first_name: str
    last_name: str
    title: str
    email: str = ""
    note: str = ""


@app.post("/api/prospects/{pid}/contact")
def add_contact(pid: int, body: ContactIn):
    st = pipe.store
    p = st.prospect(pid)
    if not (body.first_name.strip() and body.last_name.strip() and body.title.strip()):
        raise HTTPException(400, "first name, last name and title are required")
    p["data"].setdefault("manual_contacts", []).append({
        "first_name": body.first_name.strip(), "last_name": body.last_name.strip(), "position": body.title.strip(),
        "value": body.email.strip().lower(), "confidence": 100, "type": "personal", "note": body.note})
    st.save(p)
    st.log(p, 4, "contact", f"Operator added a manual contact: {body.first_name} {body.last_name}, {body.title}")
    if p["outcome"] in ("NO_SUITABLE_CONTACT", "NEEDS_SETUP", "NEEDS_HUMAN") and p["stage"] == "QUALIFIED":
        st.save(p, outcome="", outcome_reason="")
        pipe.enqueue(pid)
    return {"ok": True}


class ResolveIn(BaseModel):
    sent: bool


@app.post("/api/prospects/{pid}/resolve_send")
def resolve_send(pid: int, body: ResolveIn):
    pipe.resolve_uncertain(pid, body.sent)
    return {"ok": True}


@app.get("/api/config")
def config():
    return {k: load_config(k) for k in ("rubric_v1", "capabilities_v1", "policy_v1", "sources_v1")}
