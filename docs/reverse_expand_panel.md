# Reverse-Expand Diagnostic Panel — design spec

**Status:** spec only — implementation pending in `bench/reverse_expand_panel/`.
**Owner / driver:** Sriram.
**Tracking issue:** [codesurgeon#69](https://github.com/subsriram/codesurgeon/issues/69) (re-evaluation of the v1 ranking changes that were reverted in `5516865`).

## Why this exists

Issue #69 tried two things — density-aware fan-out and query-term-overlap fan-out for the reverse-edge BFS in `engine.rs::reverse_expand_from_anchors`. Both shipped, both were reverted on `claude/clever-leakey-7ab62d` after the agent-in-the-loop run on `sympy__sympy-21379` regressed. A v2 attempt (semantic embedding fan-out, PR #83) also failed to surface the fix site for that one task.

Two confounds make those agent-loop results untrustworthy as a referendum on the ranking:

1. **Buggy claude binary.** Phases 4c–4g ran with claude-code v2.1.117, which broke `--mcp-config` (acknowledged in commit `0d4dc95` and again in `a7c57ce`). When MCP is broken, the agent never reaches codesurgeon's tools, so its tool-call traces are not measuring the ranking change — they're measuring a degraded fallback to Grep/Read regardless of what the capsule contains.
2. **Single adversarial fixture.** `sympy-21379` is the worst-case shape for any lexical-signal ranking: user names the user-facing API (`subs`, `Piecewise`), fix site is in the modular-arithmetic kernel (`Mod.eval`), zero lexical overlap with the query by construction. Treating "doesn't help on this one task" as proof that the ranking change is wrong is overfitting to one cell of a stratified design that never got run.

This panel re-evaluates the same ranking variants with both confounds removed: **no claude, no MCP, no agent.** Just `codesurgeon context --json` against a stratified panel of tasks, scored against gold fix sites from SWE-bench Verified patches.

## Goal

For each ranking variant, measure **fix-site retrieval recall** — does the gold fix site land in capsule pivots, adjacents, or impact graph — across a panel that exercises the axes the variants are supposed to discriminate on (anchor signal type, graph density, hop distance).

The deliverable is a heatmap (variants × category cells) per metric. Reading: "v1a is uniformly worse than v0" is one possible conclusion; "v1a wins the dense+symptom cell, regresses sparse+named" is another, and the agent-in-the-loop runs flatten both into the same 0/1 outcome on a single task.

## Panel design

### Task spec

Each task is one TOML file in `bench/reverse_expand_panel/panel/tasks/`:

```toml
id            = "sympy__sympy-21379"
upstream_repo = "sympy/sympy"
base_commit   = "abc123..."
query         = "Unexpected PolynomialError when using simple substitution"
context       = """<full problem statement, including any traceback>"""
fix_sites     = ["sympy/core/mod.py::Mod::eval"]   # from gold patch
[category]
anchor   = "symptom_only"   # one of: traceback | named_api | symptom_only
density  = "dense"          # one of: sparse | medium | dense
hops     = 3                # 1 | "2-3" | "4+"
```

### Stratification

Stratify across three axes that v1/v2/v3 variants are designed to move:

| Axis | Levels | What it tests |
|---|---|---|
| Anchor signal type | `traceback` (Python frames in `context`) / `named_api` (user names a function — possibly the buggy one) / `symptom_only` (zero lexical overlap with fix site, sympy-21379 class) | Whether query-aware ranking helps (`named_api`) or actively hurts (`symptom_only`) |
| Graph density at strongest anchor | `sparse` (<10 reverse edges) / `medium` (10–50) / `dense` (>50) | Whether density-aware fan-out helps; fixed `fan_out=5` should fail on `dense` and be neutral on `sparse` |
| Fix-site hop distance | `1` (adjacent) / `2-3` / `4+` | Whether total-node-budget A\* beats fixed beam at depth |

### Panel size

Target ~20 tasks: ~3 in each (anchor × density) cell that has a non-trivial number of SWE-bench Verified examples, plus 2–3 edge cases (multi-file fixes, fix in a new file, fix in test infrastructure). Include `sympy-21379` as a known-hard cell entry, **not** as the panel.

### Panel ingestion

One-time script `bench/reverse_expand_panel/scripts/ingest_panel.py`:

1. Read SWE-bench Verified.
2. For each candidate task, parse the gold patch with `unidiff` to extract `(file_path, function_name)` pairs touched. Map function names to FQNs by reading the file at `base_commit` and matching identifier definitions.
3. Auto-classify the `category.anchor`:
   - Has `Traceback (most recent call last)` followed by `File "..."` frames → `traceback`.
   - Names at least one identifier that survives in the gold patch → `named_api`.
   - Otherwise → `symptom_only`.
4. Auto-classify `category.density` by indexing the warm workspace and counting reverse edges of the strongest anchor. Bucket into sparse/medium/dense.
5. Compute `category.hops` by `codesurgeon flow <fix_site> <anchor>` (use the existing CLI subcommand).
6. Write `tasks/<id>.toml`. Categories are auto-suggested but can be hand-corrected.

This is a one-shot tool. The point is to avoid hand-maintaining 20 specs and to be able to grow the panel mechanically when needed.

## Variants

Gate ranking strategies behind a single env var read inside `engine.rs::reverse_expand_from_anchors`. **One binary, env-gated variants** — much faster to iterate than rebuilding per variant.

```
CS_REVERSE_EXPAND_STRATEGY ∈ {
  none,         # baseline: pre-#67, no reverse-expand at all
  v0,           # current main: fixed fan_out=5, depth=3 (#67 only)
  v1a,          # density-aware fan-out alone
  v1b,          # query-term-overlap fan-out alone
  v1ab,         # density + query (v1 as originally landed in #69)
  v2,           # semantic-embedding fan-out (#83, currently default)
  v3a,          # total-node-budget + best-first, mixed-signal priority
  v3b,          # v3a + UCB exploration bonus
}
```

Re-introducing v1 logic on a feature gate is mandatory — the v1 code is currently absent from `main` (revert in `5516865`). Resurrecting it as a variant rather than as the new default is a much smaller commitment than landing a new ranking change.

## Metrics

Per `(variant, task)`:

| Metric | Type | Notes |
|---|---|---|
| `fix_site_in_pivots` | bool | Any gold fix site present in capsule pivots? |
| `fix_site_in_skeletons` | bool | Present in capsule skeletons (adjacents)? |
| `fix_site_in_impact` | bool | Run `codesurgeon impact <strongest_anchor>` separately and check there. Catches "would the agent find it via one chained MCP call" — a second-tier success state. |
| `fix_site_rank` | `Option<u32>` | Rank within capsule (pivots first, then skeletons), if present. |
| `pivot_count` | u32 | From `capsule.stats.pivot_count`. |
| `capsule_tokens` | u32 | From `capsule.stats.total_tokens`. Cost proxy. |
| `wall_ms` | u32 | Driver-measured. |

### Aggregations

- **Primary:** recall@pivots, recall@skeletons, recall@all (= pivots ∪ skeletons ∪ impact). Computed overall and per category cell.
- **Cost:** median capsule_tokens, median wall_ms (per variant). A variant that lifts recall but doubles cost may still be wrong to ship.
- **Win/loss diff log:** for each (variant, task), did it gain or lose vs. v0? This is the artifact that survives the aggregate flattening — it's the only level at which "wins on dense+symptom, loses on sparse+named" is visible.

### Multi-fix-site handling

Some gold patches touch >1 symbol. Define `fix_site_in_*` as **any** of the gold sites surfacing. Revisit if it turns out >1-site tasks dominate the panel; for now `any` is the right starting metric because the agent only needs one entry-point to start the right exploration.

## Harness shape

Lives alongside the existing swebench harness, not inside it. cs-benchmark already has the warm-workspace machinery (extracted to this repo in codesurgeon `9f39466`), so reuse `prepare_workspace.sh` and the per-task warm-cache layout.

```
cs-benchmark/
  bench/
    swebench/                       # existing
    reverse_expand_panel/           # new — this spec
      panel/
        tasks/<task_id>.toml
        variants.toml               # variant → env-var mapping
      scripts/
        ingest_panel.py             # one-shot panel ingestion
        run_panel.py                # main driver (no claude, no API)
        score.py                    # capsule + impact + gold → metrics
        report.py                   # heatmap + win/loss diff
      results/<run_id>.jsonl        # one row per (variant, task)
  docs/
    reverse_expand_panel.md         # this file
```

### Driver loop

No claude, no MCP, no API spend. Per (variant, task):

```python
env = {
    "CS_WORKSPACE": warm_workspace_for(task),
    "CS_REVERSE_EXPAND_STRATEGY": variant.strategy,
}
capsule_json = subprocess.check_output(
    [CS_BIN, "context", task.query,
     "--context", task.context,
     "--budget", "4000",
     "--json"],
    env={**os.environ, **env},
)
capsule = json.loads(capsule_json)

impact_json = subprocess.check_output(
    [CS_BIN, "impact", task.strongest_anchor, "--json"],   # see "Open items"
    env={**os.environ, **env},
)
impact = json.loads(impact_json)

metrics = score(capsule, impact, task.fix_sites)
write_result(variant, task, metrics)
```

### Resource budget

- Capsule generation: <10 s typical; <20 s on cold cache.
- 20 tasks × 8 variants ≈ 160 runs ≈ <30 min wall on warm workspaces.
- Zero API spend.

This is what makes the diagnostic worth running in the first place — agent-loop runs cost ~$1 per task per variant and need a multi-hour wall budget; this one runs in a coffee break and can be re-run on every ranking PR.

## Reading the output

The decisive question is **not** "did variant X beat v0 on average." Averages over a stratified panel collapse the structure that matters. The decisive question is **"in which category cells does each variant move recall, and at what cost?"**

Useful conclusions look like:

> v1a (density-aware) lifts recall from 11% → 67% in the `dense` density cell with no change in `sparse`/`medium`. Median capsule_tokens unchanged. Ship it.

> v1b (query-aware) lifts recall in `named_api` from 50% → 78% but drops `symptom_only` from 22% → 11%. Cost unchanged. Either ship gated on a query classifier, or don't ship.

> v3a beats v0 on `4+`-hop tasks but at 2× capsule_tokens. Worth pursuing only with budget controls.

These are conclusions the agent-in-the-loop runs structurally cannot produce — they collapse retrieval, agent prompt-following, and harness/binary version into one signal.

## Out of scope

- Agent behavior. The point of this panel is to isolate retrieval. If a variant lifts retrieval and agents still fail downstream, that's a different problem and a different harness — see the existing `bench/swebench/` for that.
- Claude binary version comparisons. The diagnostic doesn't run claude at all.
- Token cost at the agent layer. Capsule tokens are tracked; agent input/output tokens are not relevant to a retrieval question.

## Open items before implementation

1. **`codesurgeon context --json`** — landed in codesurgeon (this PR). Verify it's on the binary at `$CODESURGEON_BIN` before starting the panel work.
2. **`codesurgeon impact --json`** — does not yet exist. Either add it (small change in `cs-cli` mirroring the `context --json` pattern), or have the harness parse the markdown output for the impact step. Prefer adding `--json`.
3. **`CS_REVERSE_EXPAND_STRATEGY` env-var gate** — does not exist yet. Implementation: read once in `reverse_expand_from_anchors`, branch on the variant. Re-introduce v1 logic behind the gate (see the revert in `5516865` for the original code).
4. **SWE-bench Verified panel ingestion** — write `ingest_panel.py` first, hand-correct the auto-categories, freeze the panel before running variants. The panel must be deterministic across runs or aggregations are meaningless.
5. **Strongest-anchor extraction** — `score.py` and `ingest_panel.py` both need to identify "the anchor a reverse-expand walk would seed from." Easiest: surface this from `engine.rs` via a debug-only CLI subcommand (`codesurgeon anchors <query> --context <ctx>`). Without it the panel can still run, but `category.density` and `fix_site_in_impact` become harder to compute.

Items 1, 2, 3, 5 are codesurgeon-side. Item 4 is cs-benchmark-side. Item 1 is done; the rest are sequenced before this harness can produce its first result.
