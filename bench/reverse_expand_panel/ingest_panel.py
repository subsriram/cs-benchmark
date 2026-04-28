#!/usr/bin/env python3
"""Bootstrap panel task specs from SWE-bench Verified.

Pulls the gold patch for each requested instance, extracts touched
files + function names, and writes a `panel/tasks/<id>.toml` file
ready for hand-curation.

Auto-fills:
  - id, query, context, fix_sites, workspace, category.anchor
Hand-fill (tool prints TODOs in the file body):
  - strongest_anchor — the symbol the harness will pass to `codesurgeon impact`
  - category.density / category.hops — require an indexed workspace and the
    `codesurgeon flow` / dependents counter; left to a follow-up since this
    script intentionally does **not** assume a warm workspace exists.

Run after `prepare_workspace.sh` has already produced warm workspaces, or
re-run with `--strict` once they exist.

Usage:
    uv run bench/reverse_expand_panel/ingest_panel.py sympy__sympy-21379
    uv run bench/reverse_expand_panel/ingest_panel.py --tasks-from existing-tasks.json --limit 20

See docs/reverse_expand_panel.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

PANEL_DIR = Path(__file__).parent / "panel"
TASKS_DIR = PANEL_DIR / "tasks"

# SWE-bench Verified rows include `patch` (gold) and `test_patch` (hidden).
# Documented at https://www.swebench.com/SWE-bench/datasets.
DATASET = "princeton-nlp/SWE-bench_Verified"
CONFIG = "default"
SPLIT = "test"

TRACEBACK_RE = re.compile(r'(?m)^\s*File\s+"[^"]+",\s*line\s+\d+,\s*in\s+\w+')
DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)
PY_DEF_RE = re.compile(r"^\s*def\s+(\w+)\s*\(")
PY_CLASS_RE = re.compile(r"^\s*class\s+(\w+)")


def fetch_rows(instance_ids: list[str]) -> list[dict]:
    """Pull rows by instance_id. The HF datasets-server has no filter
    endpoint, so we paginate the full split and keep matches. Cheap
    enough for a one-shot ingestion (~500 rows in Verified).
    """
    wanted = set(instance_ids)
    out: list[dict] = []
    offset = 0
    page = 100
    while wanted:
        params = urllib.parse.urlencode(
            {
                "dataset": DATASET,
                "config": CONFIG,
                "split": SPLIT,
                "offset": offset,
                "length": page,
            }
        )
        url = f"https://datasets-server.huggingface.co/rows?{params}"
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = [r["row"] for r in data.get("rows", [])]
        if not rows:
            break
        for r in rows:
            iid = r.get("instance_id")
            if iid in wanted:
                out.append(r)
                wanted.discard(iid)
                if not wanted:
                    return out
        offset += len(rows)
    return out


def extract_fix_sites(patch: str) -> list[tuple[str, str]]:
    """Return list of `(file_path, function_name)` from a gold patch.

    Heuristic: for each `+++ b/<path>` block, scan the diff hunks for
    surrounding `def <name>` context. SWE-bench patches are usually
    small (single-function) — when not, all touched function names are
    returned and the panel author picks one.

    This is **not** a full diff parser. Use `unidiff` if this turns out
    to misclassify often.
    """
    out: list[tuple[str, str]] = []
    blocks = re.split(r"^diff --git ", patch, flags=re.M)
    for block in blocks:
        if not block.strip():
            continue
        m = DIFF_FILE_RE.search(block)
        if not m:
            continue
        path = m.group(1)
        if not path.endswith((".py", ".pyx")):
            continue
        # Walk hunks; track most-recent enclosing def/class while reading
        # context lines.
        last_class: str | None = None
        last_def: str | None = None
        for line in block.splitlines():
            if line.startswith("@@"):
                # Hunk header may contain `@@ -X,Y +X,Y @@ def func(...)`.
                m_def = re.search(r"@@.*\bdef\s+(\w+)", line)
                if m_def:
                    last_def = m_def.group(1)
                m_cls = re.search(r"@@.*\bclass\s+(\w+)", line)
                if m_cls:
                    last_class = m_cls.group(1)
                continue
            stripped = line[1:] if line[:1] in (" ", "+", "-") else line
            m_cls = PY_CLASS_RE.match(stripped)
            if m_cls:
                last_class = m_cls.group(1)
                continue
            m_def = PY_DEF_RE.match(stripped)
            if m_def:
                last_def = m_def.group(1)
                fqn = f"{path}::{last_class}::{last_def}" if last_class else f"{path}::{last_def}"
                if (path, fqn) not in out:
                    out.append((path, fqn))
        # If no enclosing def found (e.g. module-level edits), record file only.
        if not any(p == path for p, _ in out):
            out.append((path, f"{path}::<module>"))
    return out


def classify_anchor(problem_statement: str, fix_sites: list[str]) -> str:
    if TRACEBACK_RE.search(problem_statement):
        return "traceback"
    # If any fix-site name appears verbatim in the problem statement
    # (case-insensitive), the user named at least one buggy symbol.
    ps_lower = problem_statement.lower()
    for fqn in fix_sites:
        sym = fqn.rsplit("::", 1)[-1].lower()
        if len(sym) >= 4 and sym in ps_lower:
            return "named_api"
    return "symptom_only"


def write_task_toml(
    out_dir: Path,
    instance_id: str,
    repo: str,
    base_commit: str,
    problem_statement: str,
    fix_fqns: list[str],
    anchor_class: str,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{instance_id}.toml"
    # Cheap multi-line TOML — manually build to keep stdlib-only.
    fix_arr = "[" + ", ".join(_toml_str(f) for f in fix_fqns) + "]"
    body = f"""# Auto-generated by ingest_panel.py from SWE-bench Verified.
# Hand-edit `strongest_anchor`, `category.density`, `category.hops`
# before running; see docs/reverse_expand_panel.md.

id            = {_toml_str(instance_id)}
upstream_repo = {_toml_str(repo)}
base_commit   = {_toml_str(base_commit)}
workspace     = {_toml_str(f"~/projects/cs-benchmark/target/swebench-warm/{instance_id}")}

query   = {_toml_str(_summarize(problem_statement))}
context = \"\"\"{_escape_triple(problem_statement)}\"\"\"

fix_sites = {fix_arr}

# TODO(human): set to the symbol the reverse-expand walk should seed from.
# Usually an exception class or a user-named API call from `query`/`context`.
# Leave empty to skip the impact-graph metric for this task.
strongest_anchor = ""

[category]
anchor  = {_toml_str(anchor_class)}
density = "unknown"   # TODO(human): sparse | medium | dense — count
                      # `dependents(strongest_anchor)` in the indexed workspace.
hops    = "unknown"   # TODO(human): 1 | "2-3" | "4+" — run
                      # `codesurgeon flow <strongest_anchor> <fix_site>`.
"""
    out_path.write_text(body)
    return out_path


def _toml_str(s: str) -> str:
    # Minimal TOML basic-string escape. Triple-quoted form is handled
    # separately by the caller.
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_triple(s: str) -> str:
    # Avoid accidental triple-quote close inside the body.
    return s.replace('"""', '\\"\\"\\"')


def _summarize(text: str, max_chars: int = 140) -> str:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(first) <= max_chars:
        return first
    return first[: max_chars - 1] + "…"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("instance_ids", nargs="*", help="SWE-bench instance IDs.")
    p.add_argument(
        "--tasks-from",
        type=Path,
        default=None,
        help="Existing tasks.json — pull instance_ids from there.",
    )
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    ids: list[str] = list(args.instance_ids)
    if args.tasks_from:
        data = json.loads(args.tasks_from.read_text())
        for t in data.get("tasks", []):
            ids.append(t["instance_id"])
    if args.limit:
        ids = ids[: args.limit]
    if not ids:
        print("no instance_ids supplied", file=sys.stderr)
        return 2

    print(f"fetching {len(ids)} rows from {DATASET} …")
    rows = fetch_rows(ids)
    found = {r["instance_id"] for r in rows}
    missing = set(ids) - found
    if missing:
        print(f"!! not found in dataset: {sorted(missing)}", file=sys.stderr)

    for row in rows:
        iid = row["instance_id"]
        patch = row.get("patch") or ""
        sites = extract_fix_sites(patch)
        if not sites:
            print(f"  ! {iid}: no fix sites extracted from patch — skipping", file=sys.stderr)
            continue
        fix_fqns = [fqn for _path, fqn in sites]
        anchor_class = classify_anchor(row.get("problem_statement", ""), fix_fqns)
        out_path = write_task_toml(
            TASKS_DIR,
            iid,
            row["repo"],
            row["base_commit"],
            row.get("problem_statement", ""),
            fix_fqns,
            anchor_class,
        )
        print(f"  wrote {out_path.relative_to(PANEL_DIR.parent.parent)} "
              f"({len(fix_fqns)} fix sites, anchor={anchor_class})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
