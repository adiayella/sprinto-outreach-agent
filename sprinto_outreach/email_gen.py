"""Email: LLM writes parts -> code owns the layout -> separate evaluator -> 8 code checks -> at most 2 revisions."""
from __future__ import annotations

import re

from .qualification import evidence_table
from .schemas import EmailParts
from .settings import load_config

FRAMEWORK_RE = r"\b(soc ?2|iso ?27001|gdpr|hipaa|pci(?: dss)?)\b"


def build_prompt(company: str, contact: dict, pains: list[dict], evidence: list[dict], feedback: str = "") -> str:
    caps = load_config("capabilities_v1")
    pol = load_config("policy_v1")
    cap_txt = "\n".join(f'- {c["id"]}: {c["text"]}' for c in caps["capabilities"])
    pain_txt = "\n".join(f'- {p["title"]}: {p["statement"]} (capability: {p["capability_id"]}; evidence: {", ".join(p["evidence_ids"])})'
                         for p in pains)
    return f"""Write a short, human cold email from Sprinto to {contact['name']} ({contact['title']}) at {company}. Return its PARTS only.

Goal: a reply. It should read like a thoughtful person who did their homework, not a template.

Parts:
- subject: 3-7 words, specific to them, lowercase is fine. No "Re:" / "Fwd:", no clickbait.
- greeting: "Hi {contact['first_name'] or contact['name']},"
- opening_fact: ONE sentence with the most specific, interesting observation from the evidence (a hire, a launch, a
  security page detail, a customer segment). Cite it in opening_evidence_ids. Don't flatter.
- paragraphs: 1-2 short paragraphs. Connect the observation to ONE or TWO pain hypotheses, worded tentatively
  ("my guess is…", "teams at this stage often…"). Then one sentence on how Sprinto helps, using only the approved capabilities.
- question: exactly one low-pressure question (e.g. whether it's on their radar this quarter). It must be the only "?".
Total {pol['email_min_words']}-{pol['email_max_words']} words. Do not include a sign-off; code adds it.
Say each thing once: never repeat a customer name, phrase or point you've already made.
Never: invented numbers, metrics, pricing, guarantees, urgency, "following up", or saying they are not compliant.
{("Fix these problems from the last draft: " + feedback) if feedback else ""}

Pain hypotheses (guesses — keep them tentative):
{pain_txt}

Approved capabilities (the ONLY things you may say Sprinto does):
{cap_txt}

<untrusted_evidence>
{evidence_table(evidence)}
</untrusted_evidence>"""


def build_eval_prompt(email: str, contact: dict, company: str) -> str:
    return f"""You are a strict B2B sales coach. Score this cold email to {contact['name']} ({contact['title']}) at {company}.
Each criterion 0-2:
- hook: is the opening specific to this company and something a human would notice (2), generic (0)?
- hypothesis: is the pain plausible for this person and stated tentatively (2)?
- value: is the Sprinto value concrete and tied to that pain (2)?
- next_step: one clear, low-pressure question (2)?
- clarity: short, plain, no jargon or filler (2)?
Give brief notes on what to improve.

<untrusted_email>
{email}
</untrusted_email>"""


def assemble(parts: EmailParts, sender: str) -> str:
    paras = [p.strip() for p in parts.paragraphs if p.strip()][:2]
    first = parts.opening_fact.strip()
    if paras:
        first = f"{first} {paras[0]}"
        paras = paras[1:]
    blocks = [parts.greeting.strip(), first, *paras, parts.question.strip(), f"Best,\n{sender}"]
    return "\n\n".join(b for b in blocks if b)


def ensure_layout(body: str) -> str:
    """Send-time safety net: reflow a single unbroken block into paragraphs."""
    if "\n\n" in body.strip():
        return body
    sentences = re.split(r"(?<=[.?!])\s+", body.strip())
    out, cur = [], []
    for s in sentences:
        cur.append(s)
        if len(cur) == 2 or s.endswith("?"):
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return "\n\n".join(out)


def word_count(body: str) -> int:
    core = body.rsplit("\n\nBest,", 1)[0]
    return len(re.findall(r"\b[\w'’-]+\b", core))


def checks(parts: EmailParts, body: str, contact: dict, company: str, evidence: list[dict]) -> list[dict]:
    pol = load_config("policy_v1")
    caps = load_config("capabilities_v1")
    ids = {e["eid"] for e in evidence}
    ev_text = " ".join(e["excerpt"].lower() for e in evidence)
    low = body.lower()
    subject = parts.subject.lower()
    res = []

    def add(key, label, ok, detail=""):
        res.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

    first = (contact.get("first_name") or contact["name"].split()[0]).lower()
    from .extraction import clean_company_name
    company_key = clean_company_name(company).lower().split()[0].strip(",") if company.strip() else ""
    add("person", "Right person and company",
        first in parts.greeting.lower() and company_key and company_key in f"{low} {subject}",
        f"greets {contact.get('first_name') or contact['name']}, names {company}")

    open_ids = [i for i in parts.opening_evidence_ids if i in ids]
    nums = [n for n in re.findall(r"\b\d[\d,.]*\b", body.rsplit("\n\nBest,", 1)[0]) if n.strip(".,") not in ev_text]
    frameworks = set(re.findall(FRAMEWORK_RE, low))
    unsupported_fw = [f for f in frameworks if f.replace(" ", "") not in ev_text.replace(" ", "")
                      and f not in ("soc 2", "soc2", "iso 27001", "iso27001", "gdpr")]
    add("evidence", "Evidence for every factual claim", open_ids and not nums and not unsupported_fw,
        "opening cites " + (", ".join(open_ids) or "nothing") + (f"; unsupported numbers: {nums}" if nums else "")
        + (f"; unsupported frameworks: {unsupported_fw}" if unsupported_fw else ""))

    middle = " ".join(parts.paragraphs).lower()
    add("tentative", "Tentative wording for hypotheses",
        re.search(r"\b(guess|likely|probably|may|might|could|often|imagine|wonder|usually|typically|tends to)\b", middle),
        "hypothesis is phrased as a guess")

    forbidden = [w for w in caps["forbidden"] if w in low] + re.findall(
        r"\b(act now|limited time|urgent|asap|last chance|don't miss|guaranteed?)\b", low)
    add("no_invention", "No invented metrics, guarantees, pricing or urgency", not forbidden,
        ("found: " + ", ".join(sorted(set(forbidden)))) if forbidden else "clean")

    fake = re.search(r"^(re|fwd?|fw):", subject) or re.search(r"\b(following up|circling back|as discussed|per our conversation|bumping this)\b", low)
    add("no_fake_thread", "No fake “Re:” or “following up”", not fake, "")

    deficiency = re.search(r"(you('re| are) not compliant|non-?compliant|you (don't|do not) have (a )?soc|lack(s|ing)? (soc|iso|compliance))", low)
    add("no_deficiency", "No unsupported “you’re not compliant”", not deficiency, "")

    cap_ids = {c["id"] for c in caps["capabilities"]}
    cap_words = set()
    for c in caps["capabilities"]:
        cap_words |= {w for w in re.findall(r"[a-z]{5,}", c["text"])}
    mentions_sprinto = "sprinto" in low
    cap_hit = len(cap_words & set(re.findall(r"[a-z]{5,}", low))) >= 2
    bad_caps = [c for c in parts.capability_ids if c not in cap_ids]
    add("capabilities", "Only approved product capabilities", mentions_sprinto and cap_hit and not bad_caps,
        "mentions Sprinto with approved capability wording" if not bad_caps else f"unknown capability ids {bad_caps}")

    wc = word_count(body)
    q = body.count("?")
    add("length", f"{pol['email_min_words']}–{pol['email_max_words']} words, exactly one question",
        pol["email_min_words"] <= wc <= pol["email_max_words"] and q == 1, f"{wc} words, {q} question mark(s)")

    reps = repetitions(body, company, contact)
    add("no_repetition", "No repeated phrases or names", not reps,
        ("repeated: " + "; ".join(reps[:4])) if reps else "each point made once")
    return res


STOP = set("""a an the and or but if of to in on at for with by from as is are was were be been being it its this that
these those you your yours we our us i me my he she they them their there here so not no do does did have has had can could
may might will would should just also about into over than then when while which who whom what how why all any each more most
other some such only own same very s t don now teams team like""".split())
ALWAYS_OK = {"sprinto", "soc", "iso", "gdpr", "hipaa", "ccpa", "pci", "dss", "type", "ii", "hi", "best"}


def repetitions(body: str, company: str, contact: dict) -> list[str]:
    """Find a phrase (4 words, ≥2 meaningful) or a proper name said twice — e.g. listing the same customers twice."""
    core = body.rsplit("\n\nBest,", 1)[0]
    if "\n\n" in core:
        core = core.split("\n\n", 1)[1]  # skip the greeting
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.-]*[A-Za-z0-9]|[A-Za-z0-9]", core)
    low = [w.lower() for w in words]
    found: list[str] = []
    first_seen: dict = {}
    for i in range(len(low) - 3):
        g = tuple(low[i:i + 4])
        if sum(w not in STOP for w in g) < 2:
            continue
        if g in first_seen and i - first_seen[g] >= 4:
            phrase = " ".join(g)
            if phrase not in found:
                found.append(f"“{phrase}”")
        first_seen.setdefault(g, i)
    allowed = ALWAYS_OK | {w.lower() for w in re.findall(r"[A-Za-z0-9.]+", company)} | \
        {(contact.get("first_name") or "").lower()}
    counts: dict[str, int] = {}
    for w in words:
        if w[0].isupper() and w.lower() not in STOP and w.lower() not in allowed and len(w) > 2:
            counts[w] = counts.get(w, 0) + 1
    found += [f"{w} ×{n}" for w, n in counts.items() if n >= 2]
    return found
