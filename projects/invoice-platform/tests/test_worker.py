import json
from email.message import EmailMessage

import httpx

from intake.worker import Config, Pipeline, pdf_attachments, process_inbox, sender_allowed, to_panel_payload

CFG = Config(imap_host="x", imap_user="u", imap_password="p", extractor_url="http://ex", panel_url="http://panel",
             panel_token="tok")
PDF = b"%PDF-1.4 fake"


def mail(sender="billing@acme.example", attachments=(("inv.pdf", PDF),), mid="<m1@acme>"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = f"ACME Billing <{sender}>", "ap@us.example", "Invoice", mid
    m.set_content("Please find our invoice attached.")
    for name, data in attachments:
        m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return m.as_bytes()


class FakeIMAP:
    """Minimal IMAP server stand-in: UID search/fetch/store over an in-memory mailbox."""

    def __init__(self, messages):
        self.messages = {str(i + 1).encode(): m for i, m in enumerate(messages)}
        self.seen = set()

    def select(self, mailbox):
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "search":
            return "OK", [b" ".join(u for u in self.messages if u not in self.seen)]
        if cmd == "fetch":
            assert args[1] == "(BODY.PEEK[])", "must not mark mail seen while reading it"
            return "OK", [(b"1 (BODY[] {n}", self.messages[args[0]])]
        if cmd == "store":
            self.seen.add(args[0])
            return "OK", []


def services(extract=None, panel_status=201, extractor_status=200):
    calls = []
    extract = extract or {"vendor": "ACME Supplies Ltd", "number": "INV-1", "currency": "USD", "total": 10.0,
                          "lines": [], "problems": [], "ocr": False}

    def handler(req):
        calls.append(req)
        if req.url.host == "ex":
            return httpx.Response(extractor_status, json=extract)
        return httpx.Response(panel_status, json={"id": 1, "status": "pending", "updated": False})

    return Pipeline(CFG, httpx.Client(transport=httpx.MockTransport(handler))), calls


def test_files_pdf_and_marks_email_seen():
    imap = FakeIMAP([mail()])
    pipeline, calls = services()
    assert process_inbox(imap, pipeline, CFG) == {"emails": 1, "filed": 1, "skipped": 0, "retry_later": 0}
    assert imap.seen == {b"1"}
    panel = json.loads(calls[1].content)
    assert calls[1].headers["Authorization"] == "Bearer tok" and panel["number"] == "INV-1" and panel["file"] == "email:inv.pdf"


def test_outage_leaves_email_unseen_for_retry():
    imap = FakeIMAP([mail()])
    pipeline, _ = services(panel_status=503)
    assert process_inbox(imap, pipeline, CFG)["retry_later"] == 1
    assert imap.seen == set()
    pipeline, _ = services()  # panel is back
    assert process_inbox(imap, pipeline, CFG)["filed"] == 1 and imap.seen == {b"1"}


def test_already_approved_invoice_is_not_an_error():
    imap = FakeIMAP([mail()])
    pipeline, _ = services(panel_status=409)
    assert process_inbox(imap, pipeline, CFG)["filed"] == 1 and imap.seen == {b"1"}


def test_email_without_pdf_and_disallowed_sender_are_skipped():
    cfg = Config(**{**CFG.__dict__, "allowed_senders": ["@acme.example"]})
    imap = FakeIMAP([mail(attachments=()), mail(sender="spam@evil.example")])
    pipeline, calls = services()
    assert process_inbox(imap, pipeline, cfg) == {"emails": 2, "filed": 0, "skipped": 2, "retry_later": 0}
    assert calls == [] and imap.seen == {b"1", b"2"}


def test_oversized_pdf_goes_to_review_without_extraction():
    imap = FakeIMAP([mail(attachments=(("big.pdf", b"x" * (11 * 1024 * 1024)),))])
    pipeline, calls = services()
    assert process_inbox(imap, pipeline, CFG)["filed"] == 1 and imap.seen == {b"1"}
    assert [c.url.host for c in calls] == ["panel"]  # extractor never sees it
    assert "larger than 10 MB" in json.loads(calls[0].content)["problems"][0]


def test_unknown_layout_still_reaches_review_with_stable_number():
    extracted = {"vendor": None, "number": None, "total": None, "problems": ["unknown layout: add a template"], "ocr": False}
    a = to_panel_payload(extracted, "bills@newvendor.example", "<m1>", "x.pdf")
    b = to_panel_payload(extracted, "bills@newvendor.example", "<m1>", "x.pdf")
    assert a["vendor"] == "bills@newvendor.example" and a["total"] == 0 and a["problems"]
    assert a["number"] == b["number"] and a["number"].startswith("EMAIL-")  # retry updates the same item


def test_ocr_results_are_flagged_for_review():
    p = to_panel_payload({"vendor": "V", "number": "N", "total": 5, "problems": [], "ocr": True}, "s", "m", "f.pdf")
    assert p["problems"] == ["read by OCR: double-check the numbers"]


def test_sender_rules_and_attachment_names():
    assert sender_allowed("a@acme.example", ["@acme.example"]) and not sender_allowed("a@evil.example", ["@acme.example"])
    assert sender_allowed("x@y.z", [])
    import email
    from email import policy
    msg = email.message_from_bytes(mail(attachments=(("../../etc/passwd.pdf", PDF),)), policy=policy.default)
    assert pdf_attachments(msg) == [("passwd.pdf", PDF)]  # path stripped from attacker-controlled filename
