#!/usr/bin/env python3
"""Render a SWE-bench run as markdown (issue #29a).

Reads ``target/swebench/results.jsonl`` (produced by
``benches/swebench/run.py``) and emits a markdown summary to stdout.

In #29a this reports token deltas only — pass@1 columns are placeholders
until #29b wires into the ``swebench`` evaluation harness. The output
format is stable so #29b can add the pass@1 column without breaking
downstream consumers.

Usage:
    uv run scripts/swebench_report.py
    uv run scripts/swebench_report.py --results target/swebench/results.jsonl
    uv run scripts/swebench_report.py --pilot   # mark output as pilot run
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = REPO_ROOT / "target" / "swebench" / "results.jsonl"


def load_results(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def group_by_arm(results: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        out[r["arm"]].append(r)
    return out


def mean_or_none(vals: list[float | int | None]) -> float | None:
    clean = [v for v in vals if v is not None]
    if not clean:
        return None
    return statistics.mean(clean)


def sum_or_none(vals: list[float | int | None]) -> float | None:
    clean = [v for v in vals if v is not None]
    if not clean:
        return None
    return sum(clean)


def fmt_int(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{int(round(v)):,}"


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:.4f}"


def fmt_delta_pct(current: float | None, baseline: float | None) -> str:
    if current is None or baseline is None or baseline == 0:
        return "—"
    pct = (current - baseline) / baseline * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def fmt_delta_pp(current: float | None, baseline: float | None) -> str:
    if current is None or baseline is None:
        return "—"
    pp = (current - baseline) * 100
    sign = "+" if pp >= 0 else ""
    return f"{sign}{pp:.1f}pp"


def pass_rate(arm_results: list[dict]) -> float | None:
    """Compute pass@1 if eval fields are present.

    #29a does not populate ``passed`` — returns None. #29b will add this
    field after running ``swebench.harness.run_evaluation``.
    """
    with_eval = [r for r in arm_results if "passed" in r]
    if not with_eval:
        return None
    return sum(1 for r in with_eval if r["passed"]) / len(with_eval)


def _real_input(r: dict) -> int | None:
    """Total input tokens billed to the model = new + cache_creation + cache_read.

    `input_tokens` in Claude's usage payload is ONLY the new non-cached tokens
    (often 5–20 per turn). The real input bulk is in cache_creation_input_tokens
    and cache_read_input_tokens. Summing all three gives the true input token
    count that the model actually processed and that costs + latency scale with.
    """
    parts = [
        r.get("input_tokens"),
        r.get("cache_creation_tokens"),
        r.get("cache_read_tokens"),
    ]
    if all(p is None for p in parts):
        return None
    return sum(p or 0 for p in parts)


def arm_summary(arm_results: list[dict]) -> dict:
    in_tok = [_real_input(r) for r in arm_results]
    out_tok = [r.get("output_tokens") for r in arm_results]
    total_tok = [
        (_real_input(r) or 0) + (r.get("output_tokens") or 0)
        for r in arm_results
        if _real_input(r) is not None or r.get("output_tokens") is not None
    ]
    cost = [r.get("total_cost_usd") for r in arm_results]
    walltime = [r.get("walltime_s") for r in arm_results]

    return {
        "n": len(arm_results),
        "pass_rate": pass_rate(arm_results),
        "avg_input_tokens": mean_or_none(in_tok),
        "avg_output_tokens": mean_or_none(out_tok),
        "avg_total_tokens": mean_or_none(total_tok) if total_tok else None,
        "avg_cost_usd": mean_or_none(cost),
        "sum_cost_usd": sum_or_none(cost),
        "avg_walltime_s": mean_or_none(walltime),
        "errors": sum(1 for r in arm_results if r.get("error")),
    }


def render_headline(
    with_summary: dict, without_summary: dict, pilot: bool
) -> list[str]:
    header = "### SWE-bench Verified — pilot run" if pilot else "### SWE-bench Verified"
    lines = [header, ""]
    lines.append(
        f"Tasks: **{max(with_summary['n'], without_summary['n'])}**  ·  "
        f"arms: with / without codesurgeon"
    )
    if with_summary["pass_rate"] is None and without_summary["pass_rate"] is None:
        lines.append(
            "_Pass@1 not yet evaluated (harness integration in #29b)._ "
            "Token deltas below are directional only."
        )
    lines.append("")
    lines.append("| Arm | n | Pass@1 | Avg input tokens | Avg output tokens | Avg total tokens | Avg cost | Avg walltime |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for label, s in [("bare Claude Code", without_summary), ("+ codesurgeon", with_summary)]:
        pass_str = "—" if s["pass_rate"] is None else f"{s['pass_rate'] * 100:.1f}%"
        wall_str = "—" if s["avg_walltime_s"] is None else f"{s['avg_walltime_s']:.1f}s"
        lines.append(
            f"| {label} | {s['n']} | {pass_str} | "
            f"{fmt_int(s['avg_input_tokens'])} | "
            f"{fmt_int(s['avg_output_tokens'])} | "
            f"{fmt_int(s['avg_total_tokens'])} | "
            f"{fmt_usd(s['avg_cost_usd'])} | "
            f"{wall_str} |"
        )
    # Delta row relative to bare Claude Code as baseline.
    lines.append(
        f"| **Δ** | — | "
        f"{fmt_delta_pp(with_summary['pass_rate'], without_summary['pass_rate'])} | "
        f"{fmt_delta_pct(with_summary['avg_input_tokens'], without_summary['avg_input_tokens'])} | "
        f"{fmt_delta_pct(with_summary['avg_output_tokens'], without_summary['avg_output_tokens'])} | "
        f"{fmt_delta_pct(with_summary['avg_total_tokens'], without_summary['avg_total_tokens'])} | "
        f"{fmt_delta_pct(with_summary['avg_cost_usd'], without_summary['avg_cost_usd'])} | "
        f"{fmt_delta_pct(with_summary['avg_walltime_s'], without_summary['avg_walltime_s'])} |"
    )
    lines.append("")
    return lines


def render_per_repo(results: list[dict]) -> list[str]:
    """Per-repo breakdown: one row per repo, Δ columns vs bare arm."""
    by_repo: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in results:
        by_repo[r["repo"]][r["arm"]].append(r)

    if not by_repo:
        return []

    lines = ["### Per-repo breakdown", ""]
    lines.append("| Repo | Tasks | Δ avg total tokens | Δ avg cost | Δ pass@1 |")
    lines.append("|---|---:|---:|---:|---:|")
    for repo in sorted(by_repo):
        arms = by_repo[repo]
        w = arm_summary(arms.get("with", []))
        wo = arm_summary(arms.get("without", []))
        n = max(w["n"], wo["n"])
        lines.append(
            f"| {repo} | {n} | "
            f"{fmt_delta_pct(w['avg_total_tokens'], wo['avg_total_tokens'])} | "
            f"{fmt_delta_pct(w['avg_cost_usd'], wo['avg_cost_usd'])} | "
            f"{fmt_delta_pp(w['pass_rate'], wo['pass_rate'])} |"
        )
    lines.append("")
    return lines


def render_versions(results: list[dict]) -> list[str]:
    """Versions section, mirroring the panel side's ``## Build``.

    Each row stamps `(claude_version, codesurgeon_version)` from the
    binaries in use at run time (see ``benches/swebench/run.py``). Three
    states:
      - no version fields → rows pre-date the stamping change
      - one tuple → print it (provenance for the headline numbers)
      - multiple tuples → still print the section, but ALSO warn to stderr
        because mixing rows from different binaries silently invalidates
        any with/without delta. Issue #69 was caused by exactly this kind
        of silent confound (claude 2.1.117 broke ``--mcp-config`` mid-run).
    """
    pairs: dict[tuple[str, str], int] = defaultdict(int)
    for r in results:
        c = r.get("claude_version")
        s = r.get("codesurgeon_version")
        if c is None and s is None:
            continue
        pairs[(c or "", s or "")] += 1

    lines: list[str] = ["### Versions", ""]
    if not pairs:
        lines.append(
            "_No version fields on any row — results pre-date the version-stamping "
            "change in `run.py`. Provenance unverifiable._"
        )
        lines.append("")
        return lines

    if len(pairs) == 1:
        (claude, cs), n = next(iter(pairs.items()))
        lines.append(f"- claude: `{claude or '—'}`")
        lines.append(f"- codesurgeon: `{cs or '—'}`")
        lines.append("")
        return lines

    # Multiple — warn to stderr, render all in markdown.
    print(
        "## ⚠ Multiple (claude, codesurgeon) versions in result set — "
        "with/without deltas may not be valid",
        file=sys.stderr,
    )
    for (claude, cs), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        print(f"  - {n}× claude={claude or '—'} codesurgeon={cs or '—'}", file=sys.stderr)
    lines.append("⚠ **Multiple binary versions appear in this result set.** "
                 "with/without deltas mix runs from different binaries and may not be valid.")
    lines.append("")
    lines.append("| Rows | claude | codesurgeon |")
    lines.append("|---:|---|---|")
    for (claude, cs), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {n} | `{claude or '—'}` | `{cs or '—'}` |")
    lines.append("")
    return lines


def render_errors(results: list[dict]) -> list[str]:
    errored = [r for r in results if r.get("error")]
    if not errored:
        return []
    lines = ["### Errors", ""]
    lines.append(f"{len(errored)} run(s) failed:")
    lines.append("")
    for r in errored[:10]:
        err = (r.get("error") or "").splitlines()[0][:140]
        lines.append(f"- `{r['arm']}/{r['instance_id']}`: {err}")
    if len(errored) > 10:
        lines.append(f"- … and {len(errored) - 10} more")
    lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--pilot", action="store_true", help="label the report as a pilot run")
    args = parser.parse_args()

    results = load_results(args.results)
    if not results:
        print(f"no results at {args.results}", file=sys.stderr)
        return 1

    by_arm = group_by_arm(results)
    with_summary = arm_summary(by_arm.get("with", []))
    without_summary = arm_summary(by_arm.get("without", []))

    out: list[str] = []
    out.extend(render_headline(with_summary, without_summary, args.pilot))
    out.extend(render_versions(results))
    out.extend(render_per_repo(results))
    out.extend(render_errors(results))

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
