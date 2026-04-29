#!/usr/bin/env python3
"""Curate `strongest_anchor` / `category.density` / `category.hops` per task.

For each task TOML in `panel/tasks/`:
  1. If `strongest_anchor` is already set, skip (operator-locked).
  2. Run `codesurgeon anchors <query> --context <context>` to get candidate
     symbol names.
  3. Pick the first candidate that resolves in the warm workspace's index
     to a function/method/class. Filter out generic noise terms
     (`isinstance`, `len`, `print`, `open`, etc.).
  4. Compute `category.density` from `codesurgeon impact <fqn> --json`
     direct-dependent count, bucketed sparse / medium / dense.
  5. Compute `category.hops` from `codesurgeon flow <fqn> <first_fix_site>`,
     bucketed 1 / 2-3 / 4+. When flow returns no path (graph indirection),
     defaults to "2-3" with a comment.
  6. With --apply, rewrite the TOML's strongest_anchor / density / hops
     lines in place. Without --apply, prints suggestions only.

Operator locks: if you've manually set `strongest_anchor` and want to
keep it (e.g., the auto-pick chose a noise term), the helper skips that
task — it doesn't second-guess hand-curation. Density and hops are
recomputed against whatever `strongest_anchor` is set.

Usage:
    uv run bench/reverse_expand_panel/curate_anchors.py
    uv run bench/reverse_expand_panel/curate_anchors.py --apply
    uv run bench/reverse_expand_panel/curate_anchors.py --tasks task1 task2
    uv run bench/reverse_expand_panel/curate_anchors.py --override task1=Foo --override task2=Bar::baz

Output also written to `target/reverse_expand_panel/curate_suggestions.json`
for diffing against future runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from dataclasses import dataclass, asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TASKS = Path(__file__).parent / "panel" / "tasks"
DEFAULT_CS_BIN = Path(os.environ.get(
    "CODESURGEON_BIN",
    str(REPO / "target/release/codesurgeon"),
))

# Anchor candidates that are noise: too generic to seed a useful walk.
NOISE = frozenset({
    "isinstance", "len", "print", "open", "getattr", "setattr", "hasattr",
    "type", "str", "int", "float", "list", "dict", "tuple", "set",
    "Traceback", "Description", "Steps", "Reproduce", "Cell", "True", "False",
    "Output", "Input", "Note", "Bug", "Error", "Exception",
})

# Density bucketing (matches the runbook).
def density_bucket(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n < 10:
        return "sparse"
    if n <= 50:
        return "medium"
    return "dense"

# Hops bucketing.
def hops_bucket(h: int | None) -> str:
    if h is None or h < 0:
        return "2-3"  # graph indirection — pragmatic default
    if h <= 1:
        return "1"
    if h <= 3:
        return "2-3"
    return "4+"


@dataclass
class Suggestion:
    task_id: str
    candidates: list[str]
    chosen_name: str | None
    strongest_anchor: str | None
    direct_dependents: int | None
    density: str
    hops: int | None
    hops_b: str
    skipped: str | None = None  # reason if skipped (already-curated, error, etc.)

    def to_dict(self) -> dict:
        return asdict(self)


def run_cs(args: list[str], ws: Path, timeout: int = 30, stdin: str | None = None,
           cs_bin: Path = DEFAULT_CS_BIN) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CS_WORKSPACE": str(ws)}
    return subprocess.run(
        [str(cs_bin), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=stdin,
    )


def search_fqn_by_leaf(name: str, ws: Path) -> str | None:
    """Look up the largest function/method/class symbol whose leaf_name (or
    name) is `name`. Largest = most likely the user-named entity rather
    than a tiny helper.
    """
    db = ws / ".codesurgeon" / "index.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        kinds = ("function", "method", "class")
        ph = ",".join("?" * len(kinds))
        # Prefer leaf_name (Class::method form) over name field.
        for col in ("leaf_name", "name"):
            row = con.execute(
                f"""SELECT fqn FROM symbols
                    WHERE {col} = ? AND kind IN ({ph})
                    ORDER BY (end_line - start_line) DESC LIMIT 1""",
                (name, *kinds),
            ).fetchone()
            if row:
                return row[0]
        return None
    finally:
        con.close()


def anchor_candidates(query: str, context: str, ws: Path,
                      cs_bin: Path = DEFAULT_CS_BIN) -> list[str]:
    """Run `codesurgeon anchors --json` and return symbol_names."""
    proc = run_cs(
        ["anchors", query, "--context", "-", "--json"],
        ws=ws, timeout=30, stdin=context[:3000], cs_bin=cs_bin,
    )
    if proc.returncode != 0:
        return []
    try:
        return json.loads(proc.stdout).get("symbol_names", [])
    except json.JSONDecodeError:
        return []


def density(fqn: str, ws: Path, cs_bin: Path = DEFAULT_CS_BIN) -> int | None:
    proc = run_cs(["impact", fqn, "--json"], ws=ws, cs_bin=cs_bin)
    if proc.returncode != 0:
        return None
    try:
        return len(json.loads(proc.stdout).get("direct_dependents", []))
    except json.JSONDecodeError:
        return None


def flow_hops(src: str, dst: str, ws: Path,
              cs_bin: Path = DEFAULT_CS_BIN) -> int | None:
    proc = run_cs(["flow", src, dst], ws=ws, cs_bin=cs_bin)
    if proc.returncode != 0 or not proc.stdout.lstrip().startswith("Path from"):
        return None
    nodes = sum(1 for ln in proc.stdout.splitlines()
                if re.match(r"^\s*\d+\.", ln))
    return max(0, nodes - 1)


def pick_anchor(candidates: list[str], ws: Path,
                override: str | None = None,
                cs_bin: Path = DEFAULT_CS_BIN) -> tuple[str | None, str | None]:
    """Return (chosen_name, resolved_fqn). Override if provided."""
    if override:
        fqn = search_fqn_by_leaf(override, ws=ws)
        return (override, fqn)
    for name in candidates:
        if name in NOISE or len(name) < 3:
            continue
        fqn = search_fqn_by_leaf(name, ws=ws)
        if fqn:
            return (name, fqn)
    return (None, None)


def curate_one(toml_path: Path, override: str | None = None,
               recompute: bool = False,
               cs_bin: Path = DEFAULT_CS_BIN) -> Suggestion:
    data = tomllib.loads(toml_path.read_text())
    task_id = data["id"]
    ws = Path(data["workspace"]).expanduser()
    fix_sites = data.get("fix_sites", [])
    if not fix_sites:
        return Suggestion(task_id, [], None, None, None, "unknown", None, "?",
                          skipped="no fix_sites declared")
    if not (ws / ".codesurgeon" / "index.db").exists():
        return Suggestion(task_id, [], None, None, None, "unknown", None, "?",
                          skipped=f"no warm index at {ws}")

    existing_anchor = (data.get("strongest_anchor") or "").strip()

    # If anchor is already set and not recomputing, just refresh density/hops.
    if existing_anchor and not recompute and not override:
        n = density(existing_anchor, ws, cs_bin=cs_bin)
        h = flow_hops(existing_anchor, fix_sites[0], ws, cs_bin=cs_bin)
        return Suggestion(
            task_id=task_id,
            candidates=[],
            chosen_name=existing_anchor.rsplit("::", 1)[-1],
            strongest_anchor=existing_anchor,
            direct_dependents=n,
            density=density_bucket(n),
            hops=h,
            hops_b=hops_bucket(h),
            skipped="strongest_anchor already set (use --recompute to override)",
        )

    candidates = anchor_candidates(data["query"], data.get("context", ""), ws, cs_bin=cs_bin)
    chosen_name, fqn = pick_anchor(candidates, ws, override=override, cs_bin=cs_bin)

    if not fqn:
        # Last-resort: use the first fix-site's enclosing class as anchor.
        first_fix = fix_sites[0]
        if "::" in first_fix:
            cls_part, _, _ = first_fix.rpartition("::")
            if "::" in cls_part:
                # path::Class::method → path::Class
                fqn = cls_part
                chosen_name = cls_part.rsplit("::", 1)[-1]
            else:
                fqn = first_fix
                chosen_name = first_fix.rsplit("::", 1)[-1]

    n = density(fqn, ws, cs_bin=cs_bin) if fqn else None
    h = flow_hops(fqn, fix_sites[0], ws, cs_bin=cs_bin) if fqn else None

    return Suggestion(
        task_id=task_id,
        candidates=candidates[:6],
        chosen_name=chosen_name,
        strongest_anchor=fqn,
        direct_dependents=n,
        density=density_bucket(n),
        hops=h,
        hops_b=hops_bucket(h),
    )


# ─── TOML rewrite ──────────────────────────────────────────────────────────

ANCHOR_RE = re.compile(r'^strongest_anchor\s*=\s*"[^"]*"', re.M)
DENSITY_RE = re.compile(r'^density\s*=\s*"[^"]*"[^\n]*', re.M)
HOPS_RE = re.compile(r'^hops\s*=\s*"[^"]*"[^\n]*', re.M)


def apply_suggestion(toml_path: Path, s: Suggestion) -> None:
    text = toml_path.read_text()
    if s.strongest_anchor:
        text, n = ANCHOR_RE.subn(
            f'strongest_anchor = "{s.strongest_anchor}"', text, count=1)
        if n != 1:
            raise RuntimeError(f"could not rewrite strongest_anchor in {toml_path}")
    density_comment = (f"  # {s.direct_dependents} direct dependents"
                       if s.direct_dependents is not None
                       else "  # could not compute")
    hops_comment = (f"  # flow → {s.hops} hops"
                    if s.hops is not None
                    else "  # static flow has no path; fallback bucket")
    text, _ = DENSITY_RE.subn(
        f'density = "{s.density}"{density_comment}', text, count=1)
    text, _ = HOPS_RE.subn(
        f'hops    = "{s.hops_b}"{hops_comment}', text, count=1)
    toml_path.write_text(text)


# ─── CLI ───────────────────────────────────────────────────────────────────


def render(suggestions: list[Suggestion]) -> None:
    print(f"{'task':35} {'chosen':<25} {'strongest_anchor':<55} {'dep':>4} {'density':<8} {'hops':>4} {'bucket':<5}")
    print("-" * 145)
    for s in suggestions:
        if s.skipped and not s.strongest_anchor:
            print(f"  {s.task_id:33} SKIPPED — {s.skipped}")
            continue
        chosen = s.chosen_name or "?"
        fqn = s.strongest_anchor or "?"
        n = "?" if s.direct_dependents is None else str(s.direct_dependents)
        h = "?" if s.hops is None else str(s.hops)
        marker = "  " if s.skipped else "* "
        print(f"{marker}{s.task_id:33} {chosen[:23]:<25} {fqn[:53]:<55} {n:>4} {s.density:<8} {h:>4} {s.hops_b:<5}")
    print()
    if any(s.skipped for s in suggestions):
        print("(rows prefixed with '  ' are already-curated; run with --recompute to refresh density/hops anyway.)")
    print("(rows prefixed with '* ' are candidates for --apply.)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tasks", nargs="+", default=None,
                   help="Curate only these task IDs (default: all).")
    p.add_argument("--apply", action="store_true",
                   help="Write suggestions back to TOMLs. Default: dry-run only.")
    p.add_argument("--recompute", action="store_true",
                   help="Re-pick strongest_anchor even when one is already set.")
    p.add_argument("--override", action="append", default=[],
                   metavar="TASK=NAME",
                   help="Force a specific anchor leaf-name for a task. "
                        "Repeatable: --override sympy__sympy-21379=PolynomialError")
    p.add_argument("--cs-bin", type=Path, default=DEFAULT_CS_BIN)
    args = p.parse_args()

    if not args.cs_bin.exists():
        print(f"codesurgeon binary not found: {args.cs_bin}", file=sys.stderr)
        return 2

    overrides = {}
    for ov in args.override:
        if "=" not in ov:
            print(f"--override requires TASK=NAME, got: {ov}", file=sys.stderr)
            return 2
        k, v = ov.split("=", 1)
        overrides[k] = v

    paths = sorted(TASKS.glob("*.toml"))
    if args.tasks:
        wanted = set(args.tasks)
        paths = [p for p in paths if p.stem in wanted]

    suggestions = [curate_one(p, override=overrides.get(p.stem),
                              recompute=args.recompute, cs_bin=args.cs_bin)
                   for p in paths]

    render(suggestions)

    out_dir = REPO / "target" / "reverse_expand_panel"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "curate_suggestions.json").write_text(
        json.dumps([s.to_dict() for s in suggestions], indent=2, default=str))
    print(f"Wrote suggestions JSON: {out_dir / 'curate_suggestions.json'}")

    if args.apply:
        applied = 0
        for s, p in zip(suggestions, paths):
            if s.strongest_anchor and not (s.skipped and "already set" in s.skipped):
                apply_suggestion(p, s)
                applied += 1
        print(f"Applied to {applied} TOML(s). Re-run verify_fix_sites.py + run_panel.py to confirm.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
