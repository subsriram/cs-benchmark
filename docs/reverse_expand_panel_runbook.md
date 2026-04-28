# Reverse-Expand Diagnostic Panel — runbook

Companion to [`reverse_expand_panel.md`](reverse_expand_panel.md) (design + rationale). This doc is the practical end-to-end: from a fresh checkout to a heatmap.

## Prereqs

| What | Where | How to check |
|---|---|---|
| codesurgeon binary with `--json` + `CS_EXPAND_STRATEGY` support | `target/release/codesurgeon` | `target/release/codesurgeon anchors --help` (must list the `anchors` subcommand) |
| Warm workspaces — one per task, indexed under `.codesurgeon/` | `target/swebench-warm/<instance_id>/` | Each task TOML's `workspace` field points to one |
| Python 3.14 + `uv` | shell PATH | `uv --version` |
| `gh` CLI | optional, only for posting results | `gh --version` |

## 1. One-time setup

### Build codesurgeon with embeddings

```bash
cd ~/projects/codesurgeon  # or the relevant worktree
cargo build --release --features metal -p cs-core -p cs-cli -p cs-mcp
```

`metal` enables Apple Accelerate BLAS + the embeddings pipeline. The panel's `v2` variant requires embeddings; without them `v2` silently degrades to "query terms only" (still runs, but isn't measuring what you think).

### Place the binary where the harness expects it

The harness resolves the binary via `CODESURGEON_BIN`, defaulting to `$CODESURGEON_DIR/target/release/codesurgeon`. Two options:

```bash
# A. Use the codesurgeon checkout directly (default).
export CODESURGEON_DIR=~/projects/codesurgeon

# B. Bundle a binary into cs-benchmark (useful when testing a worktree
#    branch without merging it).
cp ~/projects/codesurgeon/target/release/codesurgeon ~/projects/cs-benchmark/target/release/
cp ~/projects/codesurgeon/target/release/codesurgeon-mcp ~/projects/cs-benchmark/target/release/
export CODESURGEON_BIN=~/projects/cs-benchmark/target/release/codesurgeon
```

### Install Python deps

```bash
cd ~/projects/cs-benchmark
uv sync
```

The panel scripts are stdlib-only — `uv sync` is only needed if you also plan to run the SWE-bench harness in this repo.

## 2. Build the panel

### 2a. Auto-ingest from SWE-bench Verified

```bash
uv run bench/reverse_expand_panel/ingest_panel.py \
  --tasks-from benches/swebench/tasks.json --limit 20
```

This pulls the gold patches for ~20 SWE-bench Verified instances and writes one TOML per task into `bench/reverse_expand_panel/panel/tasks/`. Each TOML is auto-filled with `id`, `query`, `context`, `fix_sites`, and `category.anchor`. The other fields are marked `TODO(human)`.

You can also pass instance IDs directly:

```bash
uv run bench/reverse_expand_panel/ingest_panel.py sympy__sympy-21379 django__django-13660
```

### 2b. Hand-curate each task TOML

For every file in `panel/tasks/`, fill three fields:

```toml
strongest_anchor = "sympy/core/mod.py::PolynomialError"   # what reverse-expand seeds from
[category]
density = "dense"     # sparse | medium | dense
hops    = 3           # 1 | "2-3" | "4+"
```

The values for `density` and `hops` are derivable using the binary itself, against the warm workspace:

```bash
# Strongest anchor candidates — usually an exception class or a named API
# from the problem statement. Confirm by extracting anchors:
CS_WORKSPACE=target/swebench-warm/sympy__sympy-21379 \
  target/release/codesurgeon anchors \
    "Unexpected PolynomialError when using simple substitution" \
    --context @target/swebench-warm/sympy__sympy-21379/problem_statement.txt \
    --json

# density — count direct dependents
CS_WORKSPACE=target/swebench-warm/sympy__sympy-21379 \
  target/release/codesurgeon impact \
    "sympy/core/mod.py::PolynomialError" --json \
    | jq '.direct_dependents | length'

# hops — codesurgeon flow
CS_WORKSPACE=target/swebench-warm/sympy__sympy-21379 \
  target/release/codesurgeon flow \
    "sympy/core/mod.py::PolynomialError" \
    "sympy/core/mod.py::Mod::eval"
```

Bucketing rule (from the design spec):

| Field | Sparse | Medium | Dense |
|---|---|---|---|
| `density` | `< 10` direct dependents | `10–50` | `> 50` |

| Field | Levels |
|---|---|
| `hops` | `1` (adjacent) / `"2-3"` / `"4+"` |

### 2c. Verify warm workspaces exist

Each task's `workspace` field must point to a directory containing both source and `.codesurgeon/index.db` + `embeddings.bin`. Reuse `benches/swebench/prepare_workspace.sh` if missing — see [`benches/swebench/WARM_WORKSPACES.md`](../benches/swebench/WARM_WORKSPACES.md).

```bash
ls target/swebench-warm/sympy__sympy-21379/.codesurgeon/index.db
ls target/swebench-warm/sympy__sympy-21379/.codesurgeon/embeddings.bin
```

If any task's workspace is missing those files, `run_panel.py` will report the row as `capsule_ok: false` and continue — but the recall measurement for that task is meaningless.

## 3. Smoke-test before the real run

Sanity-check one (variant, task) pair end-to-end:

```bash
uv run bench/reverse_expand_panel/run_panel.py \
  --variants v0 \
  --tasks sympy__sympy-21379 \
  --run-id smoke
```

Expect:
- `target/reverse_expand_panel/smoke.jsonl` exists.
- The single row in it has `capsule_ok: true` and a non-zero `pivot_count`.
- Stderr is silent (no warning lines about `unrecognized strategy`, no `! timeout`, no `! exit`).

If `pivot_count` is 0, the workspace probably isn't indexed — see step 2c.

## 4. Run the full panel

```bash
uv run bench/reverse_expand_panel/run_panel.py
```

Defaults: all variants in `panel/variants.toml`, all tasks in `panel/tasks/`, budget `4000`, fresh run-id.

Useful subsets:

```bash
# Subset of variants (e.g. just compare v0 vs v1ab vs v2)
uv run bench/reverse_expand_panel/run_panel.py --variants v0 v1ab v2

# Subset of tasks (debugging one failing cell)
uv run bench/reverse_expand_panel/run_panel.py --tasks sympy__sympy-21379

# Different budget
uv run bench/reverse_expand_panel/run_panel.py --budget 8000
```

Output goes to `target/reverse_expand_panel/<run-id>.jsonl`, one row per `(variant, task)`. Resource budget per [the design](reverse_expand_panel.md#resource-budget): ~30 minutes wall on a 20-task / 6-variant panel.

## 5. Read the report

```bash
uv run bench/reverse_expand_panel/report.py target/reverse_expand_panel/<run-id>.jsonl
```

Renders four sections:

1. **Headline recall** — overall pivots / skeletons / impact / any per variant.
2. **Per-cell heatmap** — recall per `(anchor × density × hops)` cell. This is the most important section: aggregates flatten the structure that variants are designed to discriminate.
3. **Win/loss vs. baseline** — defaults to `v0`. Override with `--baseline v2`.
4. **Cost** — median `capsule_tokens` and `wall_ms` per variant.

Multiple result files merge — useful for combining smoke + full runs:

```bash
uv run bench/reverse_expand_panel/report.py \
  target/reverse_expand_panel/*.jsonl \
  --baseline v2
```

Different metrics for the heatmap:

```bash
uv run bench/reverse_expand_panel/report.py \
  target/reverse_expand_panel/<run-id>.jsonl \
  --metric fix_site_in_skeletons   # or fix_site_in_impact
```

## 6. What to look for in the output

The decision frame from [the spec](reverse_expand_panel.md#reading-the-output):

| Pattern | Conclusion |
|---|---|
| Variant lifts recall in *every* cell vs. baseline, no cost change | Ship as default |
| Variant lifts recall in some cells, regresses others, cost flat | Either ship gated on a query classifier, or don't ship |
| Variant lifts recall but ≥ 2× cost | Pursue only with budget controls |
| Variant uniformly worse | Don't ship; investigate why before discarding the idea |

The win/loss diff is the artifact that survives aggregation. *"v1a wins on dense+symptom, loses on sparse+named"* is a different conclusion from a single percentage point.

## 7. Iteration

### Add tasks

```bash
uv run bench/reverse_expand_panel/ingest_panel.py <new_instance_id>
# hand-fill the TODO(human) fields, ensure warm workspace exists
uv run bench/reverse_expand_panel/run_panel.py --tasks <new_instance_id>
```

Re-running adds rows to a *new* JSONL file (run-id scoped). Use `report.py` with multiple files to combine.

### Add a variant

1. Add `[variants.<id>]` block to `panel/variants.toml` with the strategy string.
2. Implement that strategy in `crates/cs-core/src/ranking.rs::ExpandStrategy::from_env`. The current set is `none / v0 / v1a / v1b / v1ab / v2`; `v3a / v3b` are stubbed but not implemented.
3. Rebuild the binary, redeploy, re-run.

### Ablate budget without rebuilding

`v3a` / `v3b` (best-first walks) have an emit cap (`TOTAL_BUDGET = 40`)
and a graph-expansion cap (`EXPAND_BUDGET = 200`). Both override via
env var — useful for the [issue #96](https://github.com/subsriram/codesurgeon/issues/96)
"is best-first starving on budget?" investigation:

```bash
CS_EXPAND_TOTAL_BUDGET=200 \
CS_EXPAND_GRAPH_BUDGET=1000 \
CS_EXPAND_STRATEGY=v3a \
CS_EXPAND_DIRECTION=forward \
target/release/codesurgeon context "..." --json
```

To trace exactly what the walker emits at each depth, set `CS_LOG`:

```bash
CS_LOG=cs_core::ranking=debug target/release/codesurgeon context "..." 2>&1 | grep expand-
# expand-best-first [Forward]: emitted 8, expansions=12, depth_dist=[8, 0, 0, ...]
# expand-bfs [Reverse]: emitted 5 (cap), depth_dist=[5, 0, 0, ...]
```

`depth_dist` is a histogram indexed by depth (1, 2, 3, …). A flat
`[8, 0, 0, ...]` indicates the walk never pushed past depth 1; a
mixed `[3, 9, 5, 0, ...]` indicates depth was reached but those
candidates didn't win RRF.

### Re-run after a code change

```bash
cd ~/projects/codesurgeon
cargo build --release --features metal -p cs-core -p cs-cli
cp target/release/codesurgeon ~/projects/cs-benchmark/target/release/   # if using bundled binary

cd ~/projects/cs-benchmark
uv run bench/reverse_expand_panel/run_panel.py --run-id $(date +%Y%m%d-%H%M)
uv run bench/reverse_expand_panel/report.py target/reverse_expand_panel/<old-id>.jsonl target/reverse_expand_panel/<new-id>.jsonl
```

Diff the headline + heatmap to see what moved.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `codesurgeon binary not found` | `CODESURGEON_BIN` doesn't resolve | Build it (step 1) or override the env var |
| All rows `capsule_ok: false` | Workspace path wrong, or `.codesurgeon/` missing | Step 2c |
| `! anchors exit 1` in stderr | The `anchors` subcommand wasn't built — old binary | Rebuild from a checkout that has [PR #93](https://github.com/subsriram/codesurgeon/pull/93) |
| `CS_EXPAND_STRATEGY=… unrecognized` warning | Typo in `panel/variants.toml`, or a future variant not implemented yet | Match against the enum in `ranking.rs::ExpandStrategy::from_env`. `v3a`/`v3b` warn-and-skip on purpose. |
| `pivot_count` is 0 across all variants for one task | Anchor extraction returned nothing — usually a bad `query` or `context` | Re-run `codesurgeon anchors` directly to see what was extracted; adjust the task's `query` |
| `fix_site_in_pivots: false` everywhere but the fix is "obviously" relevant | The fix-site FQN format from the gold patch may not match what codesurgeon emits (e.g. method vs. function path separators) | Inspect `matched_fix_site` field — `null` means no match attempt succeeded. Adjust the gold FQN in the task TOML. |
| Run takes >>30 min | Either workspaces are cold (re-indexing per call) or `IMPACT_TIMEOUT_S` is being hit | Check `wall_ms` distribution in the JSONL; warm-index workspaces ahead of time |

## 9. What this doesn't measure

Stating it explicitly so it isn't conflated with what the panel claims:

- **Does not** measure agent behavior. The agent never runs.
- **Does not** measure prompt quality, claude version, or MCP transport health.
- **Does not** measure whether the agent would have *found* the fix once the capsule contains it. That's a downstream question.

The panel measures one thing: does the gold fix site land in the capsule (or one chained `impact` call away)? Answers to "should we ship this ranking change?" should be triangulated with at least one agent-loop SWE-bench run before merging — see `benches/swebench/run.py`.
