"""Poll an IMAP inbox, send every PDF attachment through the extractor, and file the result in the panel.

An email is marked \\Seen only after all of its PDFs were filed, so a crash or outage means a retry, never a loss.
Re-filing is safe: the panel upserts by vendor + invoice number and refuses to touch approved/paid invoices (409).
"""
import email
import hashlib
import imaplib
import logging
import os
import time
from dataclasses import dataclass, field
from email import policy
from email.utils import parseaddr

import httpx

log = logging.getLogger("intake")


@dataclass
class Config:
    imap_host: str
    imap_user: str
    imap_password: str
    extractor_url: str
    panel_url: str
    panel_token: str
    imap_port: int = 993
    imap_ssl: bool = True
    mailbox: str = "INBOX"
    extractor_key: str | None = None
    allowed_senders: list[str] = field(default_factory=list)  # empty = accept all; else emails or @domains
    max_pdf_mb: int = 10
    poll_seconds: int = 30

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        return cls(
            imap_host=e["IMAP_HOST"], imap_user=e["IMAP_USER"], imap_password=e["IMAP_PASSWORD"],
            extractor_url=e["EXTRACTOR_URL"], panel_url=e["PANEL_URL"], panel_token=e["PANEL_TOKEN"],
            imap_port=int(e.get("IMAP_PORT", 993)), imap_ssl=e.get("IMAP_SSL", "true").lower() == "true",
            mailbox=e.get("IMAP_MAILBOX", "INBOX"), extractor_key=e.get("EXTRACTOR_API_KEY") or None,
            allowed_senders=[s.strip().lower() for s in e.get("ALLOWED_SENDERS", "").split(",") if s.strip()],
            max_pdf_mb=int(e.get("MAX_PDF_MB", 10)), poll_seconds=int(e.get("POLL_SECONDS", 30)))


def sender_allowed(sender: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    sender = sender.lower()
    return any(sender == a or (a.startswith("@") and sender.endswith(a)) for a in allowed)


def pdf_attachments(msg: email.message.EmailMessage) -> list[tuple[str, bytes]]:
    out = []
    for part in msg.iter_attachments():
        name = part.get_filename() or "attachment.pdf"
        if part.get_content_type() == "application/pdf" or name.lower().endswith(".pdf"):
            out.append((os.path.basename(name), part.get_payload(decode=True) or b""))
    return out


def to_panel_payload(extracted: dict, sender: str, message_id: str, filename: str) -> dict:
    """The extractor's JSON is the panel's input; fill the gaps so even unreadable invoices reach human review."""
    problems = list(extracted.get("problems") or [])
    number = extracted.get("number")
    if not number:  # unknown layout: give it a stable id so a retry updates the same review item
        number = "EMAIL-" + hashlib.sha256(f"{message_id}/{filename}".encode()).hexdigest()[:10].upper()
    return {
        "vendor": extracted.get("vendor") or sender,
        "number": number,
        "date": extracted.get("date"),
        "currency": extracted.get("currency") or "USD",
        "subtotal": extracted.get("subtotal"),
        "tax": extracted.get("tax"),
        "total": extracted.get("total") or 0,
        "file": f"email:{filename}",
        "lines": extracted.get("lines") or [],
        "problems": problems + (["read by OCR: double-check the numbers"] if extracted.get("ocr") else []),
    }


class Pipeline:
    def __init__(self, cfg: Config, http: httpx.Client):
        self.cfg, self.http = cfg, http

    def file_pdf(self, sender: str, message_id: str, filename: str, data: bytes) -> str:
        if len(data) > self.cfg.max_pdf_mb * 1024 * 1024:
            # Permanent problem: retrying won't help, dropping would lose an invoice. Send it to human review.
            extracted = {"problems": [f"attachment larger than {self.cfg.max_pdf_mb} MB: not processed, open the email"]}
        else:
            headers = {"X-API-Key": self.cfg.extractor_key} if self.cfg.extractor_key else {}
            r = self.http.post(f"{self.cfg.extractor_url}/extract", files={"file": (filename, data, "application/pdf")},
                               headers=headers)
            r.raise_for_status()
            extracted = r.json()
        payload = to_panel_payload(extracted, sender, message_id, filename)
        r = self.http.post(f"{self.cfg.panel_url}/api/invoices", json=payload,
                           headers={"Authorization": f"Bearer {self.cfg.panel_token}", "Accept": "application/json"})
        if r.status_code == 409:
            return f"{payload['number']}: already approved or paid, left unchanged"
        r.raise_for_status()
        return f"{payload['number']}: {r.json()['status']}"


def process_inbox(imap, pipeline: Pipeline, cfg: Config) -> dict:
    """One pass over unseen mail. Returns counters for logging and tests."""
    stats = {"emails": 0, "filed": 0, "skipped": 0, "retry_later": 0}
    imap.select(cfg.mailbox)
    _, data = imap.uid("search", None, "UNSEEN")
    for uid in (data[0] or b"").split():
        stats["emails"] += 1
        _, parts = imap.uid("fetch", uid, "(BODY.PEEK[])")  # PEEK: reading doesn't mark it seen
        msg = email.message_from_bytes(parts[0][1], policy=policy.default)
        sender = parseaddr(str(msg.get("From", "")))[1]
        message_id = str(msg.get("Message-ID", uid.decode()))
        if not sender_allowed(sender, cfg.allowed_senders):
            log.warning("skipping email from %s: not an allowed sender", sender)
            stats["skipped"] += 1
        elif not (pdfs := pdf_attachments(msg)):
            log.info("skipping email %s from %s: no PDF attachment", message_id, sender)
            stats["skipped"] += 1
        else:
            try:
                for filename, blob in pdfs:
                    log.info("filed %s", pipeline.file_pdf(sender, message_id, filename, blob))
                    stats["filed"] += 1
            except httpx.HTTPError as e:
                log.error("email %s will be retried: %s", message_id, e)
                stats["retry_later"] += 1
                continue  # leave it unseen
        imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    return stats


def connect(cfg: Config):
    imap = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) if cfg.imap_ssl else imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
    imap.login(cfg.imap_user, cfg.imap_password)
    return imap


def run_forever(cfg: Config) -> None:
    with httpx.Client(timeout=60) as http:
        pipeline = Pipeline(cfg, http)
        while True:
            try:
                imap = connect(cfg)
                try:
                    stats = process_inbox(imap, pipeline, cfg)
                    if stats["emails"]:
                        log.info("pass done: %s", stats)
                finally:
                    imap.logout()
            except (imaplib.IMAP4.error, OSError) as e:
                log.error("mailbox unavailable, retrying in %ss: %s", cfg.poll_seconds, e)
            time.sleep(cfg.poll_seconds)
