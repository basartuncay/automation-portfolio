# Document Q&A with Citations (RAG)

Ask questions about a company's own documents (policies, handbooks, product docs, contracts) and get answers that cite the exact passage behind every claim. If the documents don't cover a question, it says so instead of making something up.

```
$ uv run python -m doc_qa samples "How long do refunds take and is shipping refunded?"

Refunds are paid back to the original payment method within 5 business days after the warehouse
receives the item [1]. Shipping costs are not refunded unless the item arrived damaged [2].
  [1] returns-policy.md › How refunds are paid: "within 5 business days after the warehouse receives the item"
  [2] returns-policy.md › How refunds are paid: "Shipping costs are not refunded unless the item arrived damaged"
```

This shows the expected output shape. The tests run offline with a stubbed model response.

## How it works

1. **Chunking:** documents are split on headings, and on paragraphs when a section is long. Each chunk keeps its heading, so it carries its context and gets a readable source name.
2. **Retrieval:** BM25 keyword ranking, implemented in about 20 lines with no vector database and no embedding API. Only chunks above a relevance threshold are used.
3. **Answering:** the top chunks go to Claude as documents with **citations enabled**, so every sentence in the answer is tied to the passage it came from by the API itself, not by fragile prompt formatting.
4. **Guardrails:**
   - no relevant chunk → "not found", with no API call and no cost
   - the system prompt forbids outside knowledge and treats document text as data, so instructions planted in a document are ignored
   - model refusals are handled

## Run it

```bash
uv sync
export ANTHROPIC_API_KEY=...          # or `ant auth login`
uv run python -m doc_qa samples "Is water damage covered by the warranty?"
uv run python -m doc_qa samples      # interactive
```

`samples/` contains three short synthetic policy documents. Point it at any folder of `.md` or `.txt` files.

## Tests

```bash
uv run pytest -q
```

They cover:
- chunking by heading and splitting long sections
- retrieval picking the right section for three different questions
- an unrelated question retrieving nothing, with no API call
- the request shape: citable documents and the injection-safe system prompt
- citation numbering with deduplicated sources
- refusals

## Scaling up for a client

- **More sources:** PDFs (pdfplumber), Google Drive, Notion or Confluence exports.
- **Bigger corpora:** swap BM25 for hybrid search (BM25 plus embeddings in pgvector or Qdrant). The `search()` interface stays the same.
- **Delivery:** a Slack bot, a website chat widget or a small FastAPI endpoint, with prompt caching on a stable document set.

Model: `claude-opus-5-5`.
