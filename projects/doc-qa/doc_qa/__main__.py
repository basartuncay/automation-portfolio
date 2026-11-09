import argparse
from pathlib import Path

import anthropic

from .core import BM25, ask, load_folder


def main() -> None:
    ap = argparse.ArgumentParser(description="Ask questions about a folder of .md/.txt documents, with citations")
    ap.add_argument("folder", type=Path)
    ap.add_argument("question", nargs="?", help="omit for an interactive prompt")
    args = ap.parse_args()

    chunks = load_folder(args.folder)
    index, client = BM25(chunks), anthropic.Anthropic()
    print(f"indexed {len(chunks)} chunks from {args.folder}")
    questions = [args.question] if args.question else iter(lambda: input("\n? ").strip(), "")
    for q in questions:
        answer, sources = ask(client, index, q)
        print("\n" + answer)
        for n, s in enumerate(sources, 1):
            print(f"  [{n}] {s}")


if __name__ == "__main__":
    main()
