import time

import pytest

from sprinto_outreach import mailer
from sprinto_outreach.urlguard import UnsafeURL, check_url, normalise_domain


def wait(pipe, rid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        ps = pipe.store.q("SELECT domain, outcome, outcome_reason FROM prospects WHERE run_id=?", (rid,))
        if all(p["outcome"] for p in ps):
            return {p["domain"]: p for p in ps}
        time.sleep(0.1)
    raise AssertionError(ps)


def test_simulated_batch(pipe):
    rid = pipe.start_run(["ledgerloop.example", "medibook.example", "crumbandco.example",
                          "stratoscale-cloud.example", "quietlabs.example"])
    out = wait(pipe, rid)
    assert out["ledgerloop.example"]["outcome"] == "SENT_SIMULATED", out["ledgerloop.example"]
    assert out["medibook.example"]["outcome"] == "SENT_SIMULATED", out["medibook.example"]
    assert out["crumbandco.example"]["outcome"] == "DISQUALIFIED"
    assert out["stratoscale-cloud.example"]["outcome"] == "LOW_PRIORITY"
    assert out["quietlabs.example"]["outcome"] == "NO_SUITABLE_CONTACT"
    led = pipe.store.prospect(pipe.store.q("SELECT id FROM prospects WHERE domain='ledgerloop.example'")[0]["id"])
    assert led["data"]["contact"]["name"] == "Priya Raman"
    assert led["data"]["rejected_citations"], "the fake hallucinated excerpt must be rejected"


def test_send_is_idempotent(pipe):
    rid = pipe.start_run(["ledgerloop.example"])
    wait(pipe, rid)
    pid = pipe.store.q("SELECT id FROM prospects")[0]["id"]
    p = pipe.store.prospect(pid)
    pipe.store.save(p, stage="DRAFT_READY", outcome="")
    pipe.enqueue(pid)
    time.sleep(1)
    assert len(pipe.store.q("SELECT * FROM sends")) == 1


def test_delivery_guard(pipe):
    with pytest.raises(mailer.DeliveryPolicyViolation):
        mailer.enforce_email_recipients(pipe.s, ["prospect@acme.com"])
    with pytest.raises(mailer.DeliveryPolicyViolation):
        mailer.enforce_email_recipients(pipe.s, ["me@example.com"], cc=["x@y.com"])


def test_url_guard():
    assert normalise_domain("https://www.Acme.com/pricing") == "acme.com"
    for bad in ("http://localhost/", "http://169.254.169.254/", "http://10.0.0.1/", "ftp://acme.com", "http://acme.com:8080/"):
        with pytest.raises(UnsafeURL):
            check_url(bad)
