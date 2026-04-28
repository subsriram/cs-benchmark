#!/usr/bin/env python3
"""Token savings micro-benchmark — binary driver (parallel to cs-core's
``examples/token_savings.rs``).

Runs a fixed set of representative queries through the ``codesurgeon``
binary against a real workspace, measures the capsule token count, and
writes a JSON file with the same shape ``scripts/bench_summary.py``
expects.

The Rust example exercises the library API in-process against a copy of
the cs-core source tree. This Python driver exercises the released binary
against an arbitrary workspace, so it can also benchmark third-party
projects (e.g. swebench task workspaces) without rebuilding.

Token estimate matches ``cs_core::capsule::estimate_tokens``: ``len // 4``
in characters. Same heuristic the Rust path uses, so the two outputs are
directly comparable.

Usage:
    uv run scripts/token_savings.py --workspace ~/projects/codesurgeon
    uv run scripts/token_savings.py \\
        --workspace ~/projects/codesurgeon \\
        --queries scripts/queries_cs_core.txt \\
        --out target/token_savings.json
    uv run scripts/token_savings.py --workspace . --budget 8000

Environment:
    CODESURGEON_DIR     path to codesurgeon source repo (default:
                        ~/projects/codesurgeon). Used to locate the
                        binary if --bin is not passed.
    CODESURGEON_BIN     full path to the codesurgeon binary
                        (default: $CODESURGEON_DIR/target/release/codesurgeon).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Default queries — the same 20 the Rust example uses, tuned to exercise
# the major code paths in cs-core. When benchmarking another project,
# pass --queries with a file of newline-separated queries instead.
DEFAULT_QUERIES = [
    "fix the retry logic",
    "add a new language parser",
    "token budget assembly",
    "how does BM25 search work",
    "session memory observations",
    "embedding cache refresh",
    "tree-sitter rust parsing",
    "sqlite schema migration",
    "graph centrality calculation",
    "rerank search results",
    "generate module documentation",
    "impact graph blast radius",
    "skeleton file API surface",
    "workspace incremental index",
    "observation staleness score",
    "stub file indexing",
    "swift enrichment xcode",
    "diff capsule for PRs",
    "search logic flow path",
    "intent detection routing",
]

# File extensions counted toward the "whole workspace" baseline. Mirrors
# the languages cs-core actually parses; reading the full tree of every
# file type would inflate the savings percentage unrealistically.
CORPUS_EXTS = {
    ".rs", ".py", ".ts", ".tsx", ".js", ".jsx",
    ".swift", ".sh", ".html", ".sql",
}

REPO_ROOT = Path(__file__).resolve().parent.parent
CODESURGEON_DIR = Path(
    os.environ.get("CODESURGEON_DIR", str(Path.home() / "projects" / "codesurgeon"))
)
DEFAULT_BIN = Path(
    os.environ.get("CODESURGEON_BIN", str(CODESURGEON_DIR / "target" / "release" / "codesurgeon"))
)
DEFAULT_OUT = REPO_ROOT / "target" / "token_savings.json"


def estimate_tokens(text: str) -> int:
    """Match ``cs_core::capsule::estimate_tokens``: chars / 4."""
    return len(text) // 4


def workspace_tokens(root: Path) -> int:
    """Sum token estimates across every parser-supported file in the corpus."""
    total = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in CORPUS_EXTS:
            continue
        # Skip vendored / build dirs that would dominate the count.
        parts = set(p.parts)
        if parts & {"target", "node_modules", ".git", "__pycache__", "dist", "build"}:
            continue
        try:
            total += estimate_tokens(p.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return total


def run_capsule(bin_path: Path, workspace: Path, query: str, budget: int) -> tuple[str, float]:
    """Invoke `codesurgeon context <query>` and capture the capsule text."""
    cmd = [
        str(bin_path),
        "--workspace", str(workspace),
        "context",
        query,
        "--budget", str(budget),
    ]
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.monotonic() - t0
    if proc.returncode != 0:
        sys.stderr.write(
            f"codesurgeon context exited {proc.returncode} for {query!r}\n"
            f"stderr: {proc.stderr.strip()}\n"
        )
        return "", elapsed
    return proc.stdout, elapsed


def load_queries(path: Path | None) -> list[str]:
    if path is None:
        return list(DEFAULT_QUERIES)
    queries: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        queries.append(line)
    if not queries:
        sys.stderr.write(f"no queries found in {path}\n")
        sys.exit(2)
    return queries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="path to the codebase to query (must already be indexed, or "
             "the first call will trigger a cold index)",
    )
    parser.add_argument(
        "--bin",
        type=Path,
        default=DEFAULT_BIN,
        help=f"codesurgeon binary (default: {DEFAULT_BIN})",
    )
    parser.add_argument(
        "--queries",
        type=Path,
        default=None,
        help="newline-separated query file (default: built-in cs-core queries)",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=4000,
        help="token budget per capsule (default: 4000, matches MCP default)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output JSON path (default: {DEFAULT_OUT.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        sys.stderr.write(f"workspace not a directory: {workspace}\n")
        return 2

    bin_path = args.bin
    if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
        sys.stderr.write(
            f"codesurgeon binary not found or not executable: {bin_path}\n"
            f"  build it: (cd {CODESURGEON_DIR} && cargo build --release --features metal)\n"
        )
        return 2

    queries = load_queries(args.queries)
    print(f"running {len(queries)} queries against {workspace}", file=sys.stderr)
    print(f"  binary: {bin_path}", file=sys.stderr)

    ws_tokens = workspace_tokens(workspace)
    print(f"  workspace tokens (corpus baseline): {ws_tokens}", file=sys.stderr)

    results: list[tuple[str, int, float]] = []
    for i, q in enumerate(queries, 1):
        capsule, elapsed = run_capsule(bin_path, workspace, q, args.budget)
        tokens = estimate_tokens(capsule)
        results.append((q, tokens, elapsed))
        print(f"  [{i:2d}/{len(queries)}] {tokens:5d} tok  {elapsed:5.2f}s  {q}", file=sys.stderr)

    total_capsule = sum(t for _, t, _ in results)
    avg_capsule = total_capsule / len(results) if results else 0.0
    savings_pct = (1.0 - avg_capsule / ws_tokens) * 100.0 if ws_tokens > 0 else 0.0

    out = {
        "workspace_tokens": ws_tokens,
        "query_count": len(results),
        "avg_capsule_tokens": round(avg_capsule, 1),
        "savings_pct": round(savings_pct, 2),
        "per_query": {q: t for q, t, _ in results},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")

    # Markdown summary mirrors the Rust example's stdout.
    print()
    print("### Token savings (binary driver)")
    print()
    print(f"Corpus: {workspace} ({ws_tokens} workspace tokens)")
    print()
    print("| Query | Capsule tokens | Latency |")
    print("|---|---:|---:|")
    for q, t, e in results:
        print(f"| {q} | {t} | {e:.2f}s |")
    print()
    print(f"**Average capsule:** {avg_capsule:.0f} tokens")
    print(f"**Workspace savings:** {savings_pct:.1f}%")
    print()
    print(f"_Written: {args.out}_")
    return 0


if __name__ == "__main__":
    sys.exit(main())
