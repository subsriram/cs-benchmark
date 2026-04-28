#!/usr/bin/env python3
"""Aggregate panel results into a heatmap + win/loss diff.

Reads one or more JSONL files produced by `run_panel.py` and prints:

  1. Per-variant headline recall (pivots / skeletons / impact / any).
  2. Per (variant × category-cell) heatmap for `fix_site_in_pivots`.
  3. Win/loss diff vs. a baseline variant (default `v0`).
  4. Cost summary: median capsule_tokens, median wall_ms.

Designed to fit in a terminal — no plotting dependency.

Usage:
    uv run bench/reverse_expand_panel/report.py target/reverse_expand_panel/<run>.jsonl
    uv run bench/reverse_expand_panel/report.py target/reverse_expand_panel/*.jsonl --baseline v2
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def load(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _variants(rows: list[dict]) -> list[str]:
    seen = []
    for r in rows:
        if r["variant"] not in seen:
            seen.append(r["variant"])
    return seen


def _tasks(rows: list[dict]) -> list[str]:
    seen = []
    for r in rows:
        if r["task"] not in seen:
            seen.append(r["task"])
    return seen


def _pct(num: int, denom: int) -> str:
    if denom == 0:
        return "  -- "
    return f"{100 * num / denom:5.1f}"


def headline(rows: list[dict]) -> None:
    print("\n## Headline recall (across all tasks)")
    print(
        f"{'variant':<18} {'strat':<6} {'dir':<8} {'pivots':>7} {'skel':>7} {'impact':>7} {'any':>7} {'tasks':>7}"
    )
    by_v: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_v[r["variant"]].append(r)
    for v in _variants(rows):
        rs = by_v[v]
        strat = rs[0].get("strategy", "?")
        direction = rs[0].get("direction", "auto")
        pivots = sum(1 for r in rs if r["fix_site_in_pivots"])
        skel = sum(1 for r in rs if r["fix_site_in_skeletons"])
        impact = sum(1 for r in rs if r["fix_site_in_impact"])
        any_hit = sum(
            1
            for r in rs
            if r["fix_site_in_pivots"] or r["fix_site_in_skeletons"] or r["fix_site_in_impact"]
        )
        n = len(rs)
        print(
            f"{v:<18} {strat:<6} {direction:<8} {_pct(pivots, n):>7} {_pct(skel, n):>7} "
            f"{_pct(impact, n):>7} {_pct(any_hit, n):>7} {n:>7}"
        )


def heatmap(rows: list[dict], metric: str = "fix_site_in_pivots") -> None:
    print(f"\n## Per-cell recall, metric = {metric}")
    cells: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        c = r["category"]
        cells[(c["anchor"], c["density"], c["hops"])].append(r)

    by_cell_variant: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        c = r["category"]
        by_cell_variant[(c["anchor"], c["density"], c["hops"], r["variant"])].append(r)

    cell_keys = sorted(cells.keys())
    variants = _variants(rows)

    header = f"{'anchor':<14} {'density':<8} {'hops':<6} " + " ".join(f"{v:>7}" for v in variants)
    print(header)
    print("-" * len(header))
    for ck in cell_keys:
        a, d, h = ck
        line = f"{a:<14} {d:<8} {h:<6} "
        cell_rows = cells[ck]
        n = len({r["task"] for r in cell_rows}) or 1
        for v in variants:
            vs = by_cell_variant[(a, d, h, v)]
            hit = sum(1 for r in vs if r[metric])
            line += f" {_pct(hit, len(vs)):>6}"
        line += f"   ({n} tasks)"
        print(line)


def diff_vs_baseline(rows: list[dict], baseline: str) -> None:
    print(f"\n## Win/loss vs. baseline = {baseline}, metric = fix_site_in_pivots")
    by_pair: dict[tuple[str, str], dict] = {(r["variant"], r["task"]): r for r in rows}
    variants = [v for v in _variants(rows) if v != baseline]
    tasks = _tasks(rows)
    print(f"{'variant':<8} {'wins':>5} {'losses':>7} {'ties':>5} {'win-tasks':<40}")
    for v in variants:
        wins, losses, ties = [], [], []
        for t in tasks:
            base = by_pair.get((baseline, t))
            new = by_pair.get((v, t))
            if not base or not new:
                continue
            b_hit = base["fix_site_in_pivots"]
            n_hit = new["fix_site_in_pivots"]
            if n_hit and not b_hit:
                wins.append(t)
            elif b_hit and not n_hit:
                losses.append(t)
            else:
                ties.append(t)
        win_str = ",".join(wins[:3]) + (",…" if len(wins) > 3 else "")
        print(f"{v:<8} {len(wins):>5} {len(losses):>7} {len(ties):>5} {win_str:<40}")


def cost(rows: list[dict]) -> None:
    print("\n## Cost (median across tasks per variant)")
    print(f"{'variant':<8} {'tokens':>10} {'wall_ms':>10}")
    by_v: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_v[r["variant"]].append(r)
    for v in _variants(rows):
        rs = by_v[v]
        toks = [r["capsule_tokens"] for r in rs if r["capsule_ok"]]
        walls = [r["wall_ms"] for r in rs if r["capsule_ok"]]
        med_t = statistics.median(toks) if toks else 0
        med_w = statistics.median(walls) if walls else 0
        print(f"{v:<8} {med_t:>10.0f} {med_w:>10.0f}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("results", type=Path, nargs="+")
    p.add_argument("--baseline", default="v0", help="Variant to diff against (default: v0).")
    p.add_argument(
        "--metric",
        default="fix_site_in_pivots",
        help="Metric for the heatmap (default: fix_site_in_pivots).",
    )
    args = p.parse_args()

    rows = load(args.results)
    if not rows:
        print("no rows loaded", file=sys.stderr)
        return 2

    builds = sorted({r.get("build_id", "") for r in rows} - {""})
    if not builds:
        print("\n## Build", flush=True)
        print("  no build_id captured (rows pre-date the version-stamping change)")
    elif len(builds) == 1:
        print("\n## Build")
        print(f"  {builds[0]}")
    else:
        print(
            "\n## ⚠ Multiple builds in result set — direct comparison may not be valid",
            file=sys.stderr,
        )
        for b in builds:
            print(f"  - {b}", file=sys.stderr)

    headline(rows)
    heatmap(rows, metric=args.metric)
    if args.baseline in _variants(rows):
        diff_vs_baseline(rows, args.baseline)
    else:
        print(f"\n## Win/loss skipped — baseline {args.baseline!r} not in results")
    cost(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
