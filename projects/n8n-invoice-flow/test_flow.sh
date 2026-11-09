#!/usr/bin/env bash
# End-to-end check: import the workflow into a throwaway n8n home, run it on sample PDFs, assert the routing.
# Needs: n8n and sqlite3 on PATH, the extractor API on 127.0.0.1:8077 (see README), a folder of sample PDFs as $1.
set -euo pipefail
IN="$(cd "$1" && pwd)"
HOME_DIR="$(mktemp -d)"
trap 'rm -rf "$HOME_DIR"' EXIT
export N8N_USER_FOLDER="$HOME_DIR" N8N_RESTRICT_FILE_ACCESS_TO="$IN" N8N_DIAGNOSTICS_ENABLED=false N8N_LOG_LEVEL=warn
sed "s#/data/invoices#$IN#" "$(dirname "$0")/invoice-flow.json" > "$HOME_DIR/flow.json"
n8n import:workflow --input="$HOME_DIR/flow.json" >/dev/null 2>&1
n8n execute --id=invoiceFlow00001 >/dev/null 2>&1
# n8n stores run data in its own "flatted" JSON format; decode it with the copy that ships with n8n.
FLATTED="$(dirname "$(readlink -f "$(command -v n8n)")")/../node_modules/flatted"
sqlite3 "$HOME_DIR/.n8n/database.sqlite" "select data from execution_data order by executionId desc limit 1" > "$HOME_DIR/run.txt"
node - "$FLATTED" "$HOME_DIR/run.txt" <<'EOF'
const [flatted, file] = process.argv.slice(2);
const run = require(flatted).parse(require("fs").readFileSync(file, "utf8")).resultData.runData;
const items = (n) => (run[n] || []).flatMap((r) => r.data.main.flat().map((i) => i.json));
const ok = items("To accounting"), review = items("To human review");
console.log("accounting:", ok.map((i) => [i.file, i.total]));
console.log("review:    ", review.map((i) => [i.file, i.reason]));
const files = (xs) => JSON.stringify(xs.map((i) => i.file).sort());
if (files(ok) !== JSON.stringify(["acme_1.pdf", "nordic_1.pdf"])) throw new Error("accounting branch wrong");
if (files(review) !== JSON.stringify(["acme_2_broken.pdf", "unknown_vendor.pdf"])) throw new Error("review branch wrong");
console.log("PASS");
EOF
