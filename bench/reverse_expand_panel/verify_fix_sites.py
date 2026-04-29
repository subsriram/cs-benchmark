#!/usr/bin/env python3
"""Verify each task's `fix_sites` against its gold patch + warm index.

For each (task TOML, gold patch, indexed warm workspace):
  1. Parse the patch to find which pre-image lines were modified per file.
  2. For each modified line, query the index for the smallest enclosing
     symbol — that's the *ground-truth* fix-site for that hunk.
  3. Compare the union of ground-truth FQNs against declared `fix_sites`.

Reports:
  - ✓ exact match
  - ⚠ overlap: declared cover the hunks but truth has more, or vice versa
  - ✗ wrong: declared fix_sites don't intersect ground-truth at all

Stdlib only. Patches cached in `target/swebench-patches/<id>.patch` so
the HF datasets-server is hit at most once per task.

Usage:
    uv run bench/reverse_expand_panel/verify_fix_sites.py
    uv run bench/reverse_expand_panel/verify_fix_sites.py --tasks sympy__sympy-21379
    uv run bench/reverse_expand_panel/verify_fix_sites.py --strict   # exit non-zero on any mismatch

Companion to `score.py`'s `_fqn_match` rules — when verification flags a
mismatch, the metric for that task may be wrong even before any engine
or scoring change.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = Path(__file__).parent / "panel"
TASKS_DIR = PANEL_DIR / "tasks"
PATCH_CACHE = REPO_ROOT / "target" / "swebench-patches"

DATASET = "princeton-nlp/SWE-bench_Verified"
CONFIG = "default"
SPLIT = "test"

# Match smallest enclosing symbol — exclude import/module-level entries
# whose span is a single line and almost never the actual fix site.
USEFUL_KINDS = ("function", "method", "class")


HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@", re.M)
DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)


@dataclass
class TaskVerify:
    task_id: str
    declared: set[str]
    truth: set[str] = field(default_factory=set)
    unresolved_declared: set[str] = field(default_factory=set)
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "ERROR"
        if not self.truth:
            return "NO_TRUTH"  # Couldn't derive truth — patch parse / index query failed
        if self.declared == self.truth:
            return "MATCH"
        if self.declared & self.truth:
            return "PARTIAL"
        return "WRONG"

    @property
    def missing(self) -> set[str]:
        """In truth but not declared — TOML undercounts the fix sites."""
        return self.truth - self.declared

    @property
    def extra(self) -> set[str]:
        """In declared but not truth — TOML names a wrong/extra symbol."""
        return self.declared - self.truth


def fetch_patch(instance_id: str) -> str:
    """Return the gold patch for `instance_id`, hitting the cache first."""
    PATCH_CACHE.mkdir(parents=True, exist_ok=True)
    cache = PATCH_CACHE / f"{instance_id}.patch"
    if cache.exists():
        return cache.read_text()

    # Pull from HF datasets-server. No filter endpoint, so paginate.
    print(f"  fetching {instance_id} from {DATASET} …", file=sys.stderr)
    for off in range(0, 600, 100):
        params = urllib.parse.urlencode(
            {"dataset": DATASET, "config": CONFIG, "split": SPLIT, "offset": off, "length": 100}
        )
        url = f"https://datasets-server.huggingface.co/rows?{params}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  ! HF fetch failed: {e}", file=sys.stderr)
            return ""
        for r in d.get("rows", []):
            row = r["row"]
            if row.get("instance_id") == instance_id:
                patch = row.get("patch") or ""
                cache.write_text(patch)
                return patch
    return ""


def parse_modified_lines(patch_text: str) -> dict[str, set[int]]:
    """Return `{file_path: {pre_image_line_numbers_modified}}`.

    "Modified" = either a `-` line (removed in the patch) or a `+` line's
    nearest pre-image neighbour (insertion point). For each hunk we walk
    its body, tracking `pre_line` per the hunk header; record every line
    that actually changed in the pre-image, plus the line *before* any
    insertion (so pure-add hunks still attribute to the right symbol).
    """
    result: dict[str, set[int]] = {}
    # Split into per-file blocks; the prefix `diff --git ` is the marker.
    for block in re.split(r"^diff --git ", patch_text, flags=re.M):
        if not block.strip():
            continue
        m = DIFF_FILE_RE.search(block)
        if not m:
            continue
        path = m.group(1).strip()
        if not path.endswith((".py", ".pyx")):
            continue
        modified = result.setdefault(path, set())
        # Iterate over hunks within this block.
        hunks = list(HUNK_RE.finditer(block))
        for i, hunk_m in enumerate(hunks):
            src_start = int(hunk_m.group(1))
            # Hunk body runs from end-of-header to next-hunk-or-eof.
            body_start = block.find("\n", hunk_m.end()) + 1
            body_end = hunks[i + 1].start() if i + 1 < len(hunks) else len(block)
            body = block[body_start:body_end]
            pre_line = src_start
            last_pre_seen: int | None = None
            for body_line in body.splitlines():
                if not body_line:
                    continue
                tag = body_line[:1]
                if tag == "-":
                    modified.add(pre_line)
                    last_pre_seen = pre_line
                    pre_line += 1
                elif tag == "+":
                    # Insertion point: attribute to nearest preceding pre-image line.
                    if last_pre_seen is not None:
                        modified.add(last_pre_seen)
                    elif pre_line > src_start:
                        modified.add(pre_line - 1)
                    else:
                        modified.add(pre_line)
                elif tag == " ":
                    last_pre_seen = pre_line
                    pre_line += 1
                # `\` (no newline at eof) and others: ignore
    return result


def smallest_covering(db: sqlite3.Connection, file_path: str, line: int) -> str | None:
    """Return the FQN of the smallest function/method/class symbol whose
    `[start_line..end_line]` covers `line` in `file_path`, or None.

    Smallest = most specific. For a class with methods inside, the method
    spans are nested in the class span; ORDER BY span ASC picks the method.
    """
    placeholders = ",".join("?" * len(USEFUL_KINDS))
    row = db.execute(
        f"""SELECT fqn FROM symbols
            WHERE file_path = ?
              AND start_line <= ?
              AND end_line   >= ?
              AND kind IN ({placeholders})
            ORDER BY (end_line - start_line) ASC
            LIMIT 1""",
        (file_path, line, line, *USEFUL_KINDS),
    ).fetchone()
    return row[0] if row else None


def fqn_resolves(db: sqlite3.Connection, fqn: str) -> bool:
    """True iff the index has a row with this exact FQN."""
    row = db.execute("SELECT 1 FROM symbols WHERE fqn = ? LIMIT 1", (fqn,)).fetchone()
    return row is not None


def verify_task(task_path: Path) -> TaskVerify:
    data = tomllib.loads(task_path.read_text())
    task_id = data["id"]
    declared = set(data.get("fix_sites", []))
    workspace = Path(data["workspace"]).expanduser()

    out = TaskVerify(task_id=task_id, declared=declared)

    db_path = workspace / ".codesurgeon" / "index.db"
    if not db_path.exists():
        out.error = f"index.db missing: {db_path}"
        return out

    patch = fetch_patch(task_id)
    if not patch:
        out.error = "patch unavailable (HF fetch failed or empty)"
        return out

    modified_by_file = parse_modified_lines(patch)
    if not modified_by_file:
        out.error = "no .py files modified by patch (or parse failed)"
        return out

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # Validate declared FQNs exist in the index.
        for fqn in declared:
            if not fqn_resolves(db, fqn):
                out.unresolved_declared.add(fqn)
        # Derive ground-truth fix sites from patch line ranges.
        for file_path, lines in modified_by_file.items():
            for line in sorted(lines):
                fqn = smallest_covering(db, file_path, line)
                if fqn is not None:
                    out.truth.add(fqn)
    finally:
        db.close()
    return out


def render(results: list[TaskVerify]) -> str:
    lines: list[str] = []
    by_status: dict[str, list[TaskVerify]] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r)

    summary = " | ".join(f"{k}={len(v)}" for k, v in sorted(by_status.items()))
    lines.append(f"## Gold-fix-site verification — {len(results)} tasks  ({summary})")
    lines.append("")

    for r in results:
        glyph = {
            "MATCH": "✓",
            "PARTIAL": "⚠",
            "WRONG": "✗",
            "NO_TRUTH": "?",
            "ERROR": "!",
        }[r.status]
        head = f"{glyph} [{r.task_id}]"
        if r.error:
            lines.append(f"{head}  ERROR — {r.error}")
            continue
        if r.unresolved_declared:
            lines.append(f"{head}  unresolved-in-index: {sorted(r.unresolved_declared)}")
        if r.status == "MATCH":
            lines.append(f"{head}  declared = truth ({len(r.truth)} fix-site{'s' if len(r.truth) != 1 else ''})")
            for fqn in sorted(r.truth):
                lines.append(f"        ✓ {fqn}")
        elif r.status == "PARTIAL":
            lines.append(f"{head}  declared overlaps but differs from truth")
            for fqn in sorted(r.declared & r.truth):
                lines.append(f"        ✓ {fqn}")
            for fqn in sorted(r.missing):
                lines.append(f"        + missing: {fqn}")
            for fqn in sorted(r.extra):
                lines.append(f"        - extra:   {fqn}")
        elif r.status == "WRONG":
            lines.append(f"{head}  declared and truth disjoint!")
            lines.append(f"        declared: {sorted(r.declared)}")
            lines.append(f"        truth:    {sorted(r.truth)}")
        elif r.status == "NO_TRUTH":
            lines.append(f"{head}  declared: {sorted(r.declared)} (couldn't derive truth)")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tasks", nargs="+", default=None, help="Verify only these task IDs.")
    p.add_argument("--strict", action="store_true", help="Exit non-zero on any non-MATCH.")
    args = p.parse_args()

    paths = sorted(TASKS_DIR.glob("*.toml"))
    if args.tasks:
        wanted = set(args.tasks)
        paths = [p for p in paths if p.stem in wanted]

    results = [verify_task(p) for p in paths]
    print(render(results))

    out_dir = REPO_ROOT / "target" / "reverse_expand_panel"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "verify_fix_sites.txt").write_text(render(results))

    if args.strict:
        bad = [r for r in results if r.status not in ("MATCH",)]
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
