#!/usr/bin/env python3
"""Reverse-expand diagnostic panel driver.

For each (variant, task) pair:
  1. Sets `CS_EXPAND_STRATEGY=<variant.strategy>` in the env.
  2. Runs `codesurgeon context <task.query> --context <task.context> --json`
     against the task's pre-warmed workspace.
  3. Runs `codesurgeon impact <task.strongest_anchor> --json` for the
     fix-site-in-impact metric.
  4. Scores the capsule + impact against the task's gold fix sites
     (see `score.py`).
  5. Appends one JSONL row per (variant, task) to
     `target/reverse_expand_panel/<run_id>.jsonl`.

No claude, no MCP, no API. Designed to run in <30 minutes wall on a
20-task / 6-variant panel.

Usage:
    uv run bench/reverse_expand_panel/run_panel.py
    uv run bench/reverse_expand_panel/run_panel.py --variants v0 v1a v2
    uv run bench/reverse_expand_panel/run_panel.py --tasks sympy__sympy-21379

See docs/reverse_expand_panel.md for the full design.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

# `score.py` lives next to this script.
sys.path.insert(0, str(Path(__file__).parent))
from score import score, Metrics  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = Path(__file__).parent / "panel"
TASKS_DIR = PANEL_DIR / "tasks"
VARIANTS_PATH = PANEL_DIR / "variants.toml"
RESULTS_DIR = REPO_ROOT / "target" / "reverse_expand_panel"

CODESURGEON_DIR = Path(
    os.environ.get("CODESURGEON_DIR", str(Path.home() / "projects" / "codesurgeon"))
)
DEFAULT_CS_BIN = CODESURGEON_DIR / "target" / "release" / "codesurgeon"

CONTEXT_TIMEOUT_S = 60
IMPACT_TIMEOUT_S = 30
DEFAULT_BUDGET = 4000


@dataclass
class Task:
    id: str
    query: str
    context: str
    fix_sites: list[str]
    strongest_anchor: str | None
    workspace: Path
    category_anchor: str
    category_density: str
    category_hops: str

    @classmethod
    def from_toml(cls, path: Path) -> "Task":
        data = tomllib.loads(path.read_text())
        category = data.get("category", {})
        ws = Path(data["workspace"]).expanduser()
        return cls(
            id=data["id"],
            query=data["query"],
            context=data.get("context", ""),
            fix_sites=list(data["fix_sites"]),
            strongest_anchor=data.get("strongest_anchor"),
            workspace=ws,
            category_anchor=str(category.get("anchor", "unknown")),
            category_density=str(category.get("density", "unknown")),
            category_hops=str(category.get("hops", "unknown")),
        )


@dataclass
class Variant:
    id: str
    strategy: str
    direction: str = "auto"   # auto | forward | reverse | both (#95)


def load_variants(only: list[str] | None) -> list[Variant]:
    cfg = tomllib.loads(VARIANTS_PATH.read_text())
    out: list[Variant] = []
    for vid, body in cfg.get("variants", {}).items():
        if only and vid not in only:
            continue
        out.append(
            Variant(
                id=vid,
                strategy=str(body["strategy"]),
                direction=str(body.get("direction", "auto")),
            )
        )
    return out


def load_tasks(only: list[str] | None) -> list[Task]:
    out: list[Task] = []
    for path in sorted(TASKS_DIR.glob("*.toml")):
        task = Task.from_toml(path)
        if only and task.id not in only:
            continue
        out.append(task)
    return out


def _run_cs(
    cs_bin: Path,
    workspace: Path,
    strategy: str,
    direction: str,
    args: list[str],
    timeout: int,
) -> dict | None:
    """Invoke `codesurgeon … --json` and parse the result.

    Returns the parsed JSON, or None on timeout / non-zero exit / parse
    error. Errors are surfaced in stderr but do not abort the panel —
    one failed (variant, task) row is preferable to losing the rest of
    the run.
    """
    env = {
        **os.environ,
        "CS_WORKSPACE": str(workspace),
        "CS_EXPAND_STRATEGY": strategy,
        "CS_EXPAND_DIRECTION": direction,
    }
    try:
        proc = subprocess.run(
            [str(cs_bin), *args, "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"  ! timeout running {args[0]} ({timeout}s)", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(
            f"  ! {args[0]} exit {proc.returncode}: {proc.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"  ! {args[0]} produced non-JSON output: {e}", file=sys.stderr)
        return None


def capture_build_id(cs_bin: Path) -> str:
    """Run `<cs_bin> --version` and return the trimmed output.

    Captured once per panel run and stamped on every result row so the
    JSONL is self-describing — `report.py` can warn if results from
    different builds are merged into one report.
    """
    try:
        out = subprocess.check_output([str(cs_bin), "--version"], text=True, timeout=5)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"  ! could not capture build id: {e}", file=sys.stderr)
        return "unknown"
    return out.strip()


def run_one(
    cs_bin: Path,
    variant: Variant,
    task: Task,
    budget: int,
    build_id: str,
) -> dict:
    t0 = time.time()
    capsule_args = [
        "context",
        task.query,
        "--budget",
        str(budget),
    ]
    if task.context:
        capsule_args += ["--context", task.context]
    capsule = _run_cs(
        cs_bin,
        task.workspace,
        variant.strategy,
        variant.direction,
        capsule_args,
        CONTEXT_TIMEOUT_S,
    )
    capsule_ms = int((time.time() - t0) * 1000)

    impact: dict | None = None
    if task.strongest_anchor:
        impact = _run_cs(
            cs_bin,
            task.workspace,
            variant.strategy,
            variant.direction,
            ["impact", task.strongest_anchor],
            IMPACT_TIMEOUT_S,
        )

    if capsule is None:
        metrics = Metrics(False, False, False, None, None, 0, 0)
    else:
        metrics = score(capsule, impact, task.fix_sites)

    return {
        "build_id": build_id,
        "variant": variant.id,
        "strategy": variant.strategy,
        "direction": variant.direction,
        "task": task.id,
        "category": {
            "anchor": task.category_anchor,
            "density": task.category_density,
            "hops": task.category_hops,
        },
        **metrics.to_dict(),
        "wall_ms": capsule_ms,
        "capsule_ok": capsule is not None,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--variants", nargs="+", default=None, help="Only run these variant IDs.")
    p.add_argument("--tasks", nargs="+", default=None, help="Only run these task IDs.")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    p.add_argument(
        "--cs-bin",
        type=Path,
        default=Path(os.environ.get("CODESURGEON_BIN", str(DEFAULT_CS_BIN))),
    )
    p.add_argument("--run-id", default=None, help="Output suffix; defaults to timestamp+uuid.")
    args = p.parse_args()

    if not args.cs_bin.exists():
        print(f"codesurgeon binary not found: {args.cs_bin}", file=sys.stderr)
        return 2

    variants = load_variants(args.variants)
    tasks = load_tasks(args.tasks)
    if not variants:
        print("no variants matched", file=sys.stderr)
        return 2
    if not tasks:
        print("no tasks matched", file=sys.stderr)
        return 2

    build_id = capture_build_id(args.cs_bin)
    print(f"build: {build_id}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    out_path = RESULTS_DIR / f"{run_id}.jsonl"
    print(f"writing results to {out_path}")

    with out_path.open("w") as fh:
        for v in variants:
            for t in tasks:
                print(f"  [{v.id}] {t.id}", flush=True)
                row = run_one(args.cs_bin, v, t, args.budget, build_id)
                fh.write(json.dumps(row) + "\n")
                fh.flush()

    print(f"done; {len(variants) * len(tasks)} rows written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
