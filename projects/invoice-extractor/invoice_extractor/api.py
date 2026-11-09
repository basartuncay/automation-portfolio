"""HTTP wrapper so no-code tools (n8n, Make, Zapier) can call the extractor: POST a PDF, get JSON back."""
import os, tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, UploadFile

from .core import process

app = FastAPI(title="Invoice Extractor API")
MAX_BYTES = 10 * 1024 * 1024


@app.post("/extract")
async def extract(file: UploadFile, x_api_key: str | None = Header(None)):
    if os.environ.get("EXTRACTOR_API_KEY") and x_api_key != os.environ["EXTRACTOR_API_KEY"]:
        raise HTTPException(401, "invalid api key")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "file larger than 10 MB")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / Path(file.filename or "invoice.pdf").name  # .name: no path traversal from the client
        path.write_bytes(data)
        inv = process(path)
    result = asdict(inv)
    result["ok"] = not inv.problems
    return result


@app.get("/health")
def health():
    return {"ok": True}
