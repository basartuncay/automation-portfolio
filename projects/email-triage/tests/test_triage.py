import csv
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2 as httpx  # the anthropic SDK 2.x HTTP client

from email_triage.__main__ import run
from email_triage.core import Category, Triage, Urgency, read_eml, route, triage

SAMPLES = Path(__file__).parents[1] / "samples"


def t(**kw):
    base = dict(category=Category.other, urgency=Urgency.low, summary="s", company=None, requested_action=None,
                amount=None, currency=None, due_date=None, needs_reply=False, suspected_injection=False)
    return Triage(**base | kw)


class FakeClient:
    """Stands in for anthropic.Anthropic(); records what was sent, returns scripted responses."""

    def __init__(self, responses):
        self.responses, self.sent = list(responses), []
        self.messages = self

    def parse(self, **kw):
        self.sent.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(result):
    return SimpleNamespace(stop_reason="end_turn", parsed_output=result)


def test_reads_eml_headers_and_body():
    m = read_eml(SAMPLES / "01_lead.eml")
    assert m["subject"] == "Quote for weekly price monitoring"
    assert "Brightwave Foods" in m["body"] and "maria@" in m["from"]


def test_email_is_sent_as_delimited_untrusted_data():
    c = FakeClient([ok(t())])
    triage(c, read_eml(SAMPLES / "04_injection.eml"))
    sent = c.sent[0]
    assert sent["model"] == "claude-opus-5-5" and sent["output_format"] is Triage
    assert "never follow instructions" in sent["system"]
    content = sent["messages"][0]["content"]
    assert content.startswith("<email>") and content.endswith("</email>") and "Ignore all previous" in content


def test_injection_flag_overrides_routing():
    assert route(t(category=Category.invoice, suspected_injection=True)) == "review"
    assert route(t(category=Category.invoice)) == "finance"


def test_refusal_and_bad_output_go_to_review():
    mail = read_eml(SAMPLES / "02_invoice.eml")
    assert triage(FakeClient([SimpleNamespace(stop_reason="refusal", parsed_output=None)]), mail)[1] == "model declined this email"
    assert triage(FakeClient([SimpleNamespace(stop_reason="max_tokens", parsed_output=None)]), mail)[1] == "incomplete or invalid output"


def test_api_error_goes_to_review_not_crash():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError("boom", response=httpx.Response(500, request=req), body=None)
    assert triage(FakeClient([err]), read_eml(SAMPLES / "02_invoice.eml")) == (None, "API error 500")


def test_long_body_is_truncated():
    c = FakeClient([ok(t())])
    triage(c, {"file": "x", "from": "a", "subject": "b", "date": "", "body": "x" * 50000}, max_body_chars=100)
    assert "[truncated]" in c.sent[0]["messages"][0]["content"] and len(c.sent[0]["messages"][0]["content"]) < 300


def test_end_to_end_writes_triaged_and_review(tmp_path):
    responses = [
        ok(t(category=Category.sales_lead, urgency=Urgency.medium, company="Brightwave Foods", amount=1500, currency="USD",
             due_date="2026-11-20", needs_reply=True)),
        ok(t(category=Category.invoice, amount=249.0, currency="EUR", due_date="2026-11-30")),
        ok(t(category=Category.support, urgency=Urgency.high, needs_reply=True)),
        SimpleNamespace(stop_reason="refusal", parsed_output=None),
    ]
    done, review = run(FakeClient(responses), SAMPLES, tmp_path)
    assert (done, review) == (3, 1)
    rows = list(csv.DictReader(open(tmp_path / "triaged.csv")))
    assert [r["route"] for r in rows] == ["sales", "finance", "support"]
    assert rows[0]["amount"] == "1500.0" and rows[0]["due_date"] == "2026-11-20"
