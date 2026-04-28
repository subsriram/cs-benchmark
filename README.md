# cs-benchmark

Benchmarks and evaluation harness for [codesurgeon](https://github.com/subsriram/codesurgeon).

Lives in its own repo so the benchmark concern (long-running SWE-bench
pilots, baseline JSON, output artifacts) doesn't intermingle with the
codesurgeon source tree.

## Layout

```
benches/
├── baseline.json            # criterion indexing-bench medians (PR diffs ref this)
├── token_baseline.json      # token-savings per-query reference
└── swebench/
    ├── tasks.json           # selected 100-task SWE-bench Verified subset
    ├── run.py               # agent driver (claude --print + MCP per arm)
    ├── select_tasks.py      # regenerate tasks.json from HuggingFace
    ├── prepare_workspace.sh # warm-index a single task workspace
    ├── mcp_with.json        # treatment arm MCP config template
    ├── mcp_without.json     # control arm MCP config (empty)
    ├── README.md            # how to run a pilot
    ├── WARM_WORKSPACES.md   # per-task warm-cache flow
    ├── smoke.md             # smoke-test recipes
    ├── report_pilot.md      # last pilot's rendered report
    └── pilot_results/       # snapshot harness JSON for committed runs

scripts/
├── bench_summary.py         # render PR-comment markdown from criterion + token bench
├── token_savings.py         # token-savings micro-bench driver (binary, parallel
│                            #   to codesurgeon's cs-core examples/token_savings.rs)
├── swebench_pilot.sh        # one-shot 3-phase pilot launcher
├── swebench_eval.py         # run swebench harness against produced diffs
├── swebench_report.py       # render pass@1 + cost markdown from results
├── swebench_quick_report.py # in-flight progress check
└── swebench_trace.py        # capture per-task trace bundles

target/                      # gitignored — all bench output
```

## Prerequisites

- A built copy of codesurgeon at `$CODESURGEON_DIR` (default
  `~/projects/codesurgeon`). Build with:
  ```bash
  cd "$CODESURGEON_DIR" && cargo build --release --features metal
  ```
- **uv** + Python 3.14 — managed via `pyproject.toml` (no system Python
  pollution)
- **Docker** — required by `swebench.harness` for evaluation
- **`claude` on PATH** — Claude Code CLI v2.1+, OAuth-authenticated

## Setup

Once per clone:

```bash
cd ~/projects/cs-benchmark
uv sync          # creates .venv/, installs swebench harness
```

After this every script can be invoked via `uv run scripts/foo.py …` (uv
picks up the project venv automatically) or by activating the venv
directly (`source .venv/bin/activate` then `python scripts/foo.py`).

## Configuration

Every script that needs a codesurgeon binary resolves it via:

| Env var               | Default                                              |
|-----------------------|------------------------------------------------------|
| `CODESURGEON_DIR`     | `~/projects/codesurgeon`                             |
| `CODESURGEON_BIN`     | `$CODESURGEON_DIR/target/release/codesurgeon`        |
| `CODESURGEON_MCP_BIN` | `$CODESURGEON_DIR/target/release/codesurgeon-mcp`    |

Override per-shell when running against a different checkout.

## Running benchmarks

### Token-savings (binary driver, ~30s)

```bash
uv run scripts/token_savings.py --workspace "$CODESURGEON_DIR"
# writes target/token_savings.json + markdown table to stdout
```

There's also a Rust version in the codesurgeon repo
(`crates/cs-core/examples/token_savings.rs`) that exercises the library
in-process. Both write the same JSON shape — the python driver lets you
benchmark arbitrary workspaces without rebuilding.

### Indexing bench summary (after a `cargo bench` run in codesurgeon)

```bash
# In the codesurgeon repo:
cd "$CODESURGEON_DIR"
cargo bench -p cs-core --bench indexing
cargo run --release --example token_savings -p cs-core

# Back here, render the PR-comment markdown:
cd ~/projects/cs-benchmark
python3 scripts/bench_summary.py
```

`bench_summary.py` reads `$CODESURGEON_DIR/target/criterion/` and
`$CODESURGEON_DIR/target/token_savings.json`, diffs against the
committed `benches/baseline.json` / `benches/token_baseline.json`, and
prints markdown to stdout.

### SWE-bench pilot

See [benches/swebench/README.md](benches/swebench/README.md). TL;DR:

```bash
nohup bash scripts/swebench_pilot.sh > /tmp/cs-swe-pilot.out 2>&1 < /dev/null &
disown
```

## Where output lands

All bench output writes under `target/` in this repo:

- `target/swebench/results.jsonl`     — agent run output (one row per task × arm)
- `target/swebench/<run-id>/`         — per-pilot logs + harness eval output
- `target/swebench-warm/<task>/`      — warm-indexed task workspaces (default)
- `target/token_savings.json`         — python token-savings driver output
- `target/criterion/`                 — only if you redirect a cargo bench here

Everything in `target/` is gitignored — re-runnable, never committed.
