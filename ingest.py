#!/usr/bin/env python3
"""Bonfires Knowledge Ingester — CLI tool to pipe content into the Bonfires knowledge graph.

Usage:
    python ingest.py text "some content" --source "live-notes"
    python ingest.py file ./notes.md --source "session-notes"
    echo "content" | python ingest.py stdin --source "pipe"
    python ingest.py triple "Subject" "predicate" "Object"
    python ingest.py search "query string"
    python ingest.py conversation ./chat-log.txt --source "discord-export"

Requires Python 3.10+, no external dependencies.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration — override with environment variables if needed
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("DELVE_API_KEY", "8n5l-sJnrHjywrTnJ3rJCjo1f1uLyTPYy_yLgq_bf-d")
BONFIRE_ID = os.environ.get("BONFIRE_ID", "698b70002849d936f4259848")
BASE_URL = os.environ.get("DELVE_BASE_URL", "https://tnt-v2.api.bonfires.ai")

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"


def c(text: str, color: str) -> str:
    """Wrap *text* in an ANSI colour code."""
    return f"{color}{text}{RESET}"


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
BANNER = f"""{c('=' * 52, DIM)}
{c('  Bonfires Knowledge Ingester', BOLD + CYAN)}
{c('  Pipe knowledge into the graph.', DIM)}
{c('=' * 52, DIM)}"""


def print_banner() -> None:
    print(BANNER)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------
def _make_request(endpoint: str, payload: dict) -> dict:
    """POST JSON to *endpoint* and return the parsed response body."""
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"error": True, "status": exc.code, "detail": body}
    except urllib.error.URLError as exc:
        return {"error": True, "detail": str(exc.reason)}


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------
def ingest_content(content: str, source: str, *, dry_run: bool = False) -> dict | None:
    """Send arbitrary text to the /ingest_content endpoint."""
    payload = {
        "bonfire_id": BONFIRE_ID,
        "content": content,
        "source": source,
    }

    if dry_run:
        _print_dry_run("POST /ingest_content", payload)
        return None

    print(c("  Ingesting content ...", DIM))
    result = _make_request("/ingest_content", payload)
    _print_result(result)
    return result


def add_triplet(subject: str, predicate: str, obj: str, *, dry_run: bool = False) -> dict | None:
    """Add a knowledge triple via /api/kg/add-triplet."""
    payload = {
        "bonfire_id": BONFIRE_ID,
        "source_node": {"name": subject, "node_type": "entity"},
        "edge": {"name": predicate, "relationship_type": predicate},
        "target_node": {"name": obj, "node_type": "entity"},
    }

    if dry_run:
        _print_dry_run("POST /api/kg/add-triplet", payload)
        return None

    print(c("  Adding triple ...", DIM))
    result = _make_request("/api/kg/add-triplet", payload)
    _print_result(result)
    return result


def search(query: str, num_results: int = 5, *, dry_run: bool = False) -> dict | None:
    """Query the knowledge graph via /delve."""
    payload = {
        "query": query,
        "bonfire_id": BONFIRE_ID,
        "num_results": num_results,
    }

    if dry_run:
        _print_dry_run("POST /delve", payload)
        return None

    print(c("  Searching ...", DIM))
    result = _make_request("/delve", payload)
    _print_search_result(result)
    return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def _print_dry_run(method: str, payload: dict) -> None:
    print()
    print(c("  [DRY RUN]", YELLOW + BOLD), c(method, YELLOW))
    print(c("  Payload:", DIM))
    for key, value in payload.items():
        display = str(value)
        if len(display) > 120:
            display = display[:117] + "..."
        print(f"    {c(key, CYAN)}: {display}")
    print()


def _print_result(result: dict) -> None:
    if result.get("error"):
        status = result.get("status", "?")
        detail = result.get("detail", "unknown error")
        print(c(f"  FAIL", RED + BOLD) + f"  status={status}")
        print(c(f"  {detail}", RED))
    elif result.get("success"):
        doc_id = result.get("document_id", "(no id returned)")
        print(c("  OK", GREEN + BOLD) + f"  document_id={c(doc_id, CYAN)}")
    else:
        # Some endpoints return a different shape — print what we got.
        print(c("  Response:", GREEN))
        print(f"    {json.dumps(result, indent=2)}")


def _print_search_result(result: dict) -> None:
    if result.get("error"):
        _print_result(result)
        return

    num = result.get("num_results", 0)
    episodes = result.get("episodes", [])
    entities = result.get("entities", [])
    edges = result.get("edges", [])

    print(c("  Search results:", GREEN + BOLD))
    print(f"  {c(str(len(episodes)), CYAN)} episodes, "
          f"{c(str(len(entities)), CYAN)} entities, "
          f"{c(str(len(edges)), CYAN)} edges")
    print()

    if episodes:
        print(c("  Episodes:", BOLD))
        for i, ep in enumerate(episodes[:10], 1):
            name = ep.get("name", "unnamed")
            content = ep.get("content", "")
            # Parse JSON content if applicable
            if isinstance(content, str) and content.startswith("{"):
                try:
                    parsed = json.loads(content)
                    content = parsed.get("content", content)
                except json.JSONDecodeError:
                    pass
            # Truncate long content
            if len(str(content)) > 200:
                content = str(content)[:200] + "..."
            print(f"    {c(f'[{i}]', BOLD)} {c(name, CYAN)}")
            if content:
                for line in str(content).splitlines()[:3]:
                    print(f"        {line}")
            print()

    if entities:
        print(c("  Entities:", BOLD))
        for ent in entities[:10]:
            name = ent.get("name", "unnamed")
            print(f"    {c('*', YELLOW)} {name}")
        print()

    if edges:
        print(c("  Relationships:", BOLD))
        for edge in edges[:10]:
            name = edge.get("name", edge.get("fact", ""))
            print(f"    {c('->', MAGENTA)} {name}")
        print()


# ---------------------------------------------------------------------------
# File & conversation readers
# ---------------------------------------------------------------------------
def read_file(path: str) -> str:
    """Read a file and return its contents."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        print(c(f"  Error: file not found: {p}", RED))
        sys.exit(1)
    return p.read_text(encoding="utf-8")


def format_conversation(raw_text: str) -> str:
    """Format alternating lines as a conversation transcript.

    Blank lines are preserved as paragraph breaks. Each non-blank line is
    prefixed with an alternating speaker label (A / B).
    """
    lines = raw_text.strip().splitlines()
    formatted: list[str] = []
    speaker_index = 0
    speakers = ["A", "B"]

    for line in lines:
        stripped = line.strip()
        if not stripped:
            formatted.append("")
            continue
        speaker = speakers[speaker_index % len(speakers)]
        formatted.append(f"[{speaker}]: {stripped}")
        speaker_index += 1

    return "\n".join(formatted)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="Bonfires Knowledge Ingester — pipe content into the knowledge graph.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be sent without making any API calls.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- text ---
    p_text = subparsers.add_parser("text", help="Ingest a text string directly.")
    p_text.add_argument("content", help="The text content to ingest.")
    p_text.add_argument("--source", default="cli", help="Source label (default: cli).")

    # --- file ---
    p_file = subparsers.add_parser("file", help="Ingest the contents of a file.")
    p_file.add_argument("path", help="Path to the file to ingest.")
    p_file.add_argument("--source", default="file", help="Source label (default: file).")

    # --- stdin ---
    p_stdin = subparsers.add_parser("stdin", help="Ingest from stdin (pipe-friendly).")
    p_stdin.add_argument("--source", default="stdin", help="Source label (default: stdin).")

    # --- triple ---
    p_triple = subparsers.add_parser("triple", help="Add a knowledge triple to the graph.")
    p_triple.add_argument("subject", help="Triple subject.")
    p_triple.add_argument("predicate", help="Triple predicate / relation.")
    p_triple.add_argument("object", help="Triple object.")

    # --- search ---
    p_search = subparsers.add_parser("search", help="Search the knowledge graph.")
    p_search.add_argument("query", help="Search query string.")
    p_search.add_argument(
        "-n", "--num-results", type=int, default=5, help="Number of results (default: 5)."
    )

    # --- conversation ---
    p_conv = subparsers.add_parser("conversation", help="Ingest a conversation transcript.")
    p_conv.add_argument("path", help="Path to the conversation text file.")
    p_conv.add_argument("--source", default="conversation", help="Source label (default: conversation).")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    print_banner()

    match args.command:
        case "text":
            preview = args.content[:80] + ("..." if len(args.content) > 80 else "")
            print(f"\n  {c('Mode:', BOLD)} text")
            print(f"  {c('Source:', BOLD)} {args.source}")
            print(f"  {c('Content:', BOLD)} {preview}\n")
            ingest_content(args.content, args.source, dry_run=args.dry_run)

        case "file":
            content = read_file(args.path)
            chars = len(content)
            lines = content.count("\n")
            print(f"\n  {c('Mode:', BOLD)} file")
            print(f"  {c('Source:', BOLD)} {args.source}")
            print(f"  {c('File:', BOLD)} {args.path}  ({lines} lines, {chars} chars)\n")
            ingest_content(content, args.source, dry_run=args.dry_run)

        case "stdin":
            if sys.stdin.isatty():
                print(c("\n  Reading from stdin (Ctrl-D to finish) ...\n", DIM), file=sys.stderr)
            content = sys.stdin.read()
            if not content.strip():
                print(c("  Error: no input received on stdin.", RED))
                sys.exit(1)
            chars = len(content)
            print(f"\n  {c('Mode:', BOLD)} stdin")
            print(f"  {c('Source:', BOLD)} {args.source}")
            print(f"  {c('Received:', BOLD)} {chars} chars\n")
            ingest_content(content, args.source, dry_run=args.dry_run)

        case "triple":
            print(f"\n  {c('Mode:', BOLD)} triple")
            print(f"  {c('Triple:', BOLD)} ({args.subject}) --[{args.predicate}]--> ({args.object})\n")
            add_triplet(args.subject, args.predicate, args.object, dry_run=args.dry_run)

        case "search":
            print(f"\n  {c('Mode:', BOLD)} search")
            print(f"  {c('Query:', BOLD)} {args.query}")
            print(f"  {c('Results:', BOLD)} {args.num_results}\n")
            search(args.query, args.num_results, dry_run=args.dry_run)

        case "conversation":
            raw = read_file(args.path)
            content = format_conversation(raw)
            lines = content.count("\n") + 1
            print(f"\n  {c('Mode:', BOLD)} conversation")
            print(f"  {c('Source:', BOLD)} {args.source}")
            print(f"  {c('File:', BOLD)} {args.path}  ({lines} lines)\n")
            # Show a short preview of the formatted conversation
            preview_lines = content.splitlines()[:4]
            for pl in preview_lines:
                print(f"    {c(pl, DIM)}")
            if len(content.splitlines()) > 4:
                print(c(f"    ... ({len(content.splitlines()) - 4} more lines)", DIM))
            print()
            ingest_content(content, args.source, dry_run=args.dry_run)

    print()


if __name__ == "__main__":
    main()
