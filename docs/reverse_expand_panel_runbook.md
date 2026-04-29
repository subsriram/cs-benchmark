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

The fastest path is the in-tree helper that does anchor-extraction + density + hops in one pass:

```bash
# Suggest values for every TOML missing strongest_anchor (dry-run; default).
uv run bench/reverse_expand_panel/curate_anchors.py

# Apply suggestions to TOMLs (review the dry-run output first).
uv run bench/reverse_expand_panel/curate_anchors.py --apply
```

The helper picks the first anchor candidate from `codesurgeon anchors` that resolves in the index, computes density via `impact`, computes hops via `flow`. It's heuristic — review the suggestions before applying, especially when the anchor classifier returned `symptom_only` or the user-named class is buried in noise terms (`isinstance`, `len`, etc.).

Manual derivation steps (used by the helper internally — runnable directly when the helper's pick is wrong):

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

### 2b'. Verify gold fix-sites (mandatory before trusting recall)

The auto-extractor in `ingest_panel.py` is heuristic — it reads diff hunks and hunk-header `def` annotations to guess fix-site FQNs. On the first 20-task ingest, **60% of auto-declared fix-sites were wrong** (mostly missing class prefix, e.g. `request` instead of `Session::request`). Without verification, any recall metric is bogus.

`verify_fix_sites.py` is the gate. For each task it:
1. Fetches the gold patch from SWE-bench Verified (cached in `target/swebench-patches/`)
2. Parses the patch to find pre-image lines modified per `.py` file
3. For each modified line, queries the warm-workspace index for the smallest enclosing function/method/class — that's the ground-truth fix-site
4. Compares against declared `fix_sites` and reports MATCH / PARTIAL / WRONG / NO_TRUTH / ERROR

```bash
# Review (dry-run): show every mismatch but don't change TOMLs
uv run bench/reverse_expand_panel/verify_fix_sites.py

# Apply: rewrite each non-MATCH task's `fix_sites = [...]` to the truth set
uv run bench/reverse_expand_panel/verify_fix_sites.py --apply

# Strict gate (CI-able): exit non-zero on any non-MATCH
uv run bench/reverse_expand_panel/verify_fix_sites.py --strict
```

After `--apply`, re-run without `--apply` to confirm all MATCH. Output also written to `target/reverse_expand_panel/verify_fix_sites.txt` for post-hoc audit.

What the verifier catches that the extractor misses:
- Missing class prefix (`request` → `Session::request`)
- Wrong method (extractor walked into a closure or sibling)
- Missing fix-sites (patch touches multiple symbols, declared only one)
- Patch inserts a new method between class methods (no covering symbol; falls back to enclosing class via hunk-header hint)

What it can't catch (rare): semantically-equivalent edits where the extractor names a wrapper but truth is the wrapped function — same line numbers, different symbol identity. Manual inspection only.

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
2. Implement that strategy in `crates/cs-core/src/ranking.rs::ExpandStrategy::from_env`. The current set is `none / v0 / v1a / v1b / v1ab / v2 / v3a / v3b` — all implemented; `is_unimplemented()` returns false for everything.
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
CS_LOG=cs_core::ranking=debug target/release/codesurgeon context "..."
# stderr (codesurgeon logs go to stderr to avoid polluting --json stdout):
#
# seed-lookup: name="hist" → leaf=2 exact=1 (union)
# expand seeds: 2 forward = [.../pyplot.py::hist, .../axes/_axes.py::Axes::hist];
#               0 reverse = [] (candidates considered: 8)
# expand-best-first [Forward]: emitted 200 (cap), depth_dist=[7, 48, 145, 0, ...]
#   seed=12345 (.../pyplot.py::hist) depth_dist=[3, 24, 73, 0, ...]
#   seed=67890 (.../axes/_axes.py::Axes::hist) depth_dist=[4, 24, 72, 0, ...]
# expand-best-first emissions: lib/.../Axes::hist::do_thing, lib/.../helper, ...
```

The four log lines surface different layers of the retrieval pipeline:

| Line | What it tells you |
|---|---|
| `seed-lookup: name=… → leaf=N exact=M` | How many symbols matched the anchor name. `leaf` uses the new indexed `leaf_name` column (catches `Class::method`); `exact` matches the literal `name` field. If `leaf == exact == 0`, the anchor name didn't resolve at all → check anchor extraction with `codesurgeon anchors`. If `leaf > exact`, the leaf-name fix [issue #96] is doing its job. |
| `expand seeds: …` | Final list of seeds chosen for the walk after the kind/fan-out gates. `candidates considered` is the pre-gate union size — when it's much bigger than `forward + reverse`, the gates dropped some. |
| `depth_dist=[…]` (per-walker) | Histogram indexed by depth (1, 2, 3, …). Flat `[8, 0, 0, ...]` → walk never pushed past depth 1; mixed → walk reached depth N but candidates didn't win RRF. |
| `seed=ID (FQN) depth_dist=[…]` | Per-seed bucketed depths. Sorted by total emissions per seed so the dominant subtree shows first. Decisive for "did seed X actually get walked, or did seed Y monopolize the budget?" |
| `<walker> emissions: fqn1, fqn2, …` | Full pre-RRF emission set. Grep for an expected fix-site name to disambiguate "walk found it but RRF dropped it" from "walk never traversed there". |

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
| `CS_EXPAND_STRATEGY=… unrecognized` warning | Typo in `panel/variants.toml`, or a future variant not implemented yet | Match against the enum in `ranking.rs::ExpandStrategy::from_env`. The current set: `none / v0 / v1a / v1b / v1ab / v2 / v3a / v3b`. |
| `expand seeds: 1 forward = [pyplot.py::hist]` (one seed for a multi-match name) | DB lookup found the wrapper (top-level function) but missed class methods. Pre-#96 binaries had this bug — `WHERE name = "hist"` only matches `name = "hist"`, not `name = "Axes::hist"`. | Verify the binary is post-`909047d54491` (fix uses `leaf_name` column). Existing warm workspaces auto-migrate on first reopen — no re-index needed; the schema migration backfills `leaf_name` for all existing rows. |
| `seed-lookup: name="X" → leaf=0 exact=0` | Anchor extraction produced "X" but no symbol in the index has it as `name` or `leaf_name`. | Either anchor extraction is too eager (run `codesurgeon anchors <query>` to inspect), or the symbol genuinely isn't indexed (re-index the workspace). |
| `pivot_count` is 0 across all variants for one task | Anchor extraction returned nothing — usually a bad `query` or `context` | Re-run `codesurgeon anchors` directly to see what was extracted; adjust the task's `query` |
| `fix_site_in_pivots: false` everywhere but the fix is "obviously" relevant | The fix-site FQN format from the gold patch may not match what codesurgeon emits (e.g. method vs. function path separators) | Inspect `matched_fix_site` field — `null` means no match attempt succeeded. Adjust the gold FQN in the task TOML. |
| Run takes >>30 min | Either workspaces are cold (re-indexing per call) or `IMPACT_TIMEOUT_S` is being hit | Check `wall_ms` distribution in the JSONL; warm-index workspaces ahead of time |

## 9. Resumption: after engine changes

When picking up the panel after a codesurgeon engine change (or after time away), this is the sequence. The current panel state is captured in [`reverse_expand_panel_findings.md`](reverse_expand_panel_findings.md); start there for what's been measured and what's still open.

### Current state (snapshot)

- 20 tasks committed under `bench/reverse_expand_panel/panel/tasks/`, distributed across 11 `(anchor × density × hops)` cells
- All 20 fix-sites verified by `verify_fix_sites.py` against gold patches — every modified line in the gold is covered by the declared fix-site set
- `strongest_anchor` / `density` / `hops` curated for all 20 against warm workspaces
- 15 variants in `panel/variants.toml`: 8 strategies × `auto` direction + (v2/v3a/v3b) × {forward, both, reverse_only}
- Last result: `target/reverse_expand_panel/20260429-1629-20task.jsonl` (gitignored under `target/`; rerun to regenerate)
- Recommendation deliverable: [`reverse_expand_panel_findings.md`](reverse_expand_panel_findings.md) (in-tree mirror of [codesurgeon#69 comment-4347417418](https://github.com/subsriram/codesurgeon/issues/69#issuecomment-4347417418))

### Trigger conditions for re-evaluation

The panel should be re-run when **any** of these happens:

| Trigger | Why | What changes |
|---|---|---|
| codesurgeon ships per-seed RRF list split (open in [#96](https://github.com/subsriram/codesurgeon/issues/96)) | Current expand variants leave 7 of 20 tasks at 0% across all variants; per-seed RRF should unstick at least 2-3 of them | v3 family may flip from "niche" to "ship as default" — the [findings.md](reverse_expand_panel_findings.md) recommendation may need revision |
| New `CS_EXPAND_STRATEGY` variants added to `ranking.rs` | New variants need ablation against the existing matrix | Add new `[variants.<id>]` blocks to `panel/variants.toml`; re-run |
| Embedding model changed (`v2` strategy) | `v2` uses semantic similarity for fan-out — new model = different ranking | Re-run; v2-related variants will move |
| Anchor extraction logic in `crates/cs-core/src/anchors.rs` changed | Different anchor sets → different seeds → different walks | Re-run; might also need to re-curate `strongest_anchor` for some tasks |
| Adding tasks beyond 20 | Per-cell n grows; previously single-task cells become more reliable | Curate new tasks per §2; re-run; compare to previous result file |

### The re-run procedure (one binary update → fresh data)

```bash
cd ~/projects/cs-benchmark
git pull

# 1. New binary in place
cp ~/projects/codesurgeon/target/release/codesurgeon target/release/
cp ~/projects/codesurgeon/target/release/codesurgeon-mcp target/release/
target/release/codesurgeon --version  # confirm sha changed

# 2. Verify gold still matches (existing TOMLs against new index — should
#    be no-op unless symbol naming convention changed)
uv run bench/reverse_expand_panel/verify_fix_sites.py --strict
# expected: 20/20 MATCH; if not, the leaf_name / FQN convention shifted

# 3. Run the full panel
CODESURGEON_BIN=$PWD/target/release/codesurgeon \
uv run bench/reverse_expand_panel/run_panel.py \
  --run-id $(date +%Y%m%d-%H%M)-<descriptive-suffix>

# 4. Render report — diff against the prior run-id
uv run bench/reverse_expand_panel/report.py \
  target/reverse_expand_panel/20260429-1629-20task.jsonl \
  target/reverse_expand_panel/<new-run-id>.jsonl \
  --baseline none

# 5. Update findings.md if recall moved meaningfully (i.e. a variant flipped
#    win/loss vs. baseline on >=2 tasks). The GH-side mirror on #69 should
#    also be updated — link points here.
```

### What to look for in the new results

The cells most likely to move on a per-seed RRF or similar engine change:

| Task | Expected to flip | Why |
|---|---|---|
| matplotlib-24177 | `fix_site_in_pivots: True` for v3-forward variants | static forward path `Axes::hist → fill → add_patch → _update_patch_limits` exists; per-seed RRF should keep `Axes::hist`'s subtree from being outranked by `pyplot::hist`'s wrapper noise |
| django-16938 | same | similar shape: `handle_m2m_field` chain |
| sklearn-25102 | already True on v3; may extend to v2-family | only v3 reaches it currently |

Tasks that **shouldn't** move from any RRF or fusion change:

| Task | Why won't move | What would |
|---|---|---|
| sympy-21379 | reverse-shaped; needs the ranking that pulls `Mod::eval` from depth-2 reverse-walk; no static forward path (anchor `PolynomialError` doesn't *call* anything) | A different ranking signal that rewards reverse-walk emissions, OR a query-aware classifier that picks reverse-only on this query shape |
| astropy-13236 / pytest-7236 | graph indirection (registry callbacks, dynamic dispatch) | A heuristic for runtime dispatch patterns; out of scope for static-graph retrieval |
| astropy-14309 | registry callback (`self._identifiers[(fmt, cls)]`) is not a static call edge | Same |

### Scaling the panel

To grow beyond 20 tasks:

```bash
# Pick + ingest more candidates
uv run bench/reverse_expand_panel/ingest_panel.py <id1> <id2> ...

# Build warm workspaces (long; ~15 min each)
for iid in <id1> <id2> ...; do
  benches/swebench/prepare_workspace.sh "$iid"
done

# Verify gold (catches extractor bugs)
uv run bench/reverse_expand_panel/verify_fix_sites.py --apply
uv run bench/reverse_expand_panel/verify_fix_sites.py --strict

# Curate strongest_anchor / density / hops
uv run bench/reverse_expand_panel/curate_anchors.py --apply

# Re-run
CODESURGEON_BIN=$PWD/target/release/codesurgeon \
uv run bench/reverse_expand_panel/run_panel.py \
  --run-id $(date +%Y%m%d-%H%M)-<descriptive-suffix>
```

Targeting unfilled cells (the 16 currently empty ones) gives more signal than adding tasks to already-occupied cells. The design spec lists the 27-cell grid — see §"Stratification".

## 10. What this doesn't measure

Stating it explicitly so it isn't conflated with what the panel claims:

- **Does not** measure agent behavior. The agent never runs.
- **Does not** measure prompt quality, claude version, or MCP transport health.
- **Does not** measure whether the agent would have *found* the fix once the capsule contains it. That's a downstream question.

The panel measures: does the gold fix site land in the capsule (`fix_site_in_pivots` / `_in_skeletons`) or one chained MCP call away (`fix_site_in_impact` for the reverse direction; `fix_site_in_forward_reach` for the forward direction)? Answers to "should we ship this ranking change?" should be triangulated with at least one agent-loop SWE-bench run before merging — see `benches/swebench/run.py`.
