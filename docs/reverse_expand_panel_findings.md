# Reverse-Expand Diagnostic Panel — current findings

**Status:** snapshot as of `codesurgeon 1.0.0 (sha 3e6c379b57f8, built 2026-04-29T13:49:13Z)` against the 20-task / 11-cell panel. Run id: `20260429-1629-20task`.

**Audience:** anyone deciding which `CS_EXPAND_STRATEGY` × `CS_EXPAND_DIRECTION` to ship as a default, or evaluating whether codesurgeon's reverse/forward expand machinery has earned its place.

**See also:**
- [`reverse_expand_panel.md`](reverse_expand_panel.md) — design spec
- [`reverse_expand_panel_runbook.md`](reverse_expand_panel_runbook.md) — how to run / resume / re-evaluate
- [codesurgeon#69 comment](https://github.com/subsriram/codesurgeon/issues/69#issuecomment-4347417418) — the GitHub-side mirror of this doc (cross-link kept in sync)
- [codesurgeon#95](https://github.com/subsriram/codesurgeon/issues/95) — bidirectional expand (closed)
- [codesurgeon#96](https://github.com/subsriram/codesurgeon/issues/96) — open follow-up: per-seed RRF list split

## TL;DR — recommendation

**Stay on `none` (no reverse-expand) as the default ranking strategy.**

The panel finds no expand variant that net-beats baseline retrieval (BM25 + ANN + anchor extraction) on the "any" metric (pivots ∪ skeletons ∪ impact ∪ forward_reach). `none` lands at **70% any-recall** across 20 tasks; the best expand variants tie, most strictly regress.

| Variant | wins-vs-none | losses-vs-none | net | verdict |
|---|---:|---:|---:|---|
| **`none` (baseline)** | — | — | — | **default — recommended** |
| **`v2_reverse_only`** | 0 | 0 | 0 | acceptable alternative; ties exactly across all 20 tasks |
| `v3a` / `v3b` (auto direction) | 1 | 1 | 0 | niche — wins sklearn-25102 (deep callee chain), loses astropy-14508 (named class direct hit) |
| `v3*_forward_only` / `v3*_both` | 1 | 1 | 0 | same niche shape |
| `v0`, `v1a`, `v1b`, `v1ab`, `v2` (auto) | 0 | 1 | **−1** | strict regression — lose a task without winning any |
| `v2_forward_only`, `v2_both` | 0 | 1 | −1 | strict regression |

The decision frame from the spec maps to: most variants land in "Don't ship; investigate why before discarding the idea." `v3` lands in "Either ship gated on a query classifier, or don't ship." `v2_reverse_only` lands in "ship if cost matters" (it ties recall, marginally cheaper tokens).

## Per-cell detail

The panel covers 11 of the 27 cells in the design's stratification grid. Cells with > 1 task can show variant differentiation; single-task cells are landmarks.

| anchor × density × hops | n | none | v0/v1*/v2 (auto) | v3 family | observation |
|---|---:|---:|---:|---:|---|
| `named_api / medium / 1` | 2 | 100% | 100% | 100% | user-named, easy retrieval; everything works |
| `named_api / sparse / 1` | 1 | 100% | 100% | mostly 100% | v3a `auto` direction misroutes to forward → 0% (classifier bug) |
| `named_api / medium / 2-3` | 1 | 100% | 0% (except v3b) | 0–100% | astropy-14508 — named class, fix one method down. Expand variants dilute it. |
| `symptom_only / sparse / 1` | 1 | 100% | 100% | 100% | sphinx-9711, anchor IS fix site |
| `symptom_only / dense / 1` | 1 | 100% | 100% (v0/v1a) / 0% (rest) | 0–100% | astropy-8872 — class-pivot match fires for v0/v1a; v2/v3 over-trim |
| **`symptom_only / dense / 2-3`** | **3** | **0%** | **0%** | **33%** | **the cell where v3 earns its place — sklearn-25102 lands** |
| `symptom_only / sparse / 2-3` | 2 | 0% | 0% | 0% | astropy-13236, pytest-7236 — graph indirection unreachable in any walk |
| `traceback / sparse / 1` | 2 | **100%** | 50% | 50% | astropy-14182, django-16454 — expand variants regress vs. baseline |
| `traceback / sparse / 2-3` | 2 | 0% | 0% | 0% | astropy-14309 (registry dispatch), django-16938 (queryset_iterator) |
| `traceback / medium / 2-3` | 2 | 50% | 50% | 50% | psf-requests-1724 lands; sympy-17630 doesn't |
| `traceback / dense / 2-3` | 3 | **100%** | 100% | 66.7% | v3 forward/both variants regress 1 of 3 |

**Two patterns visible at this resolution:**

1. **`none` is the safest configuration where the user names the buggy symbol directly.** BM25 + ANN already retrieve the symbol. The expand walks dilute the top candidates with graph neighbors, pushing the named hit out of the budget. This is the dominant failure mode for v0/v1/v2 — they hurt cases they weren't designed for.

2. **`v3` best-first wins exactly one cell** — `symptom_only / dense / 2-3` (sklearn-25102). The fix is `BaseEstimator::_validate_data` + `SelectorMixin::_transform`, deep callee chain from a public anchor; v3's priority queue pops the right children where v0/v1/v2's BFS+fan_out=5 starves.

## Why the variants don't earn their place — yet

The original [#69 thread](https://github.com/subsriram/codesurgeon/issues/69) tried two reverse-expand ranking improvements (density-aware + query-term-overlap fan-out) and reverted both after a single agent-loop regression on sympy-21379. This panel was built to re-evaluate that decision under controlled conditions (no claude, no MCP, no agent — pure retrieval scoring).

The verdict:
- **The revert was correct under partial information.** The variants do regress on tasks where the user names the symbol directly.
- **They also don't help on tasks where they were supposed to.** On the `dense` density cells the v1 work was tuned for, `none` is at 0% or 100% — the v1 variants don't move the needle.
- **The 7 tasks stuck at 0% across every variant** include 4 with verified static forward paths from anchor to fix-site. They're reachable in principle; they don't land in pivots due to per-seed RRF dilution (open in [#96](https://github.com/subsriram/codesurgeon/issues/96)).

## Engine work that would change the picture

[codesurgeon#96](https://github.com/subsriram/codesurgeon/issues/96) tracks the open per-seed RRF list split. The current `EXPAND_DEEP_RRF_K=8` change splits the deep emissions into a single re-weighted list, but multiple seeds compete in that list — `pyplot::hist`'s caller-side helpers outrank `Axes::hist`'s real chain by virtue of being reachable from more seeds.

When per-seed RRF lands, expect at least these tasks to move:
- **matplotlib-24177** (forward chain `Axes::hist → fill → add_patch → _update_patch_limits`) — should land in v3-family forward variants
- **django-16938** (forward chain `handle_m2m_field → ... → queryset.iterator`) — same shape
- Possibly **sklearn-25102** under v2-family forward (currently only v3 reaches it)

If those flip, the recommendation flips:
- v3 family (with the `auto` classifier fixed) becomes a real default candidate
- v0/v1/v2 still strict regressions; deprecate them from variants.toml
- `none` becomes "fallback for queries without strong anchor extraction"

The runbook's [Resumption section](reverse_expand_panel_runbook.md#resumption-after-engine-changes) has the trigger condition + re-run procedure.

## Methodology

| Component | Source |
|---|---|
| Tasks | 20 from SWE-bench Verified (subset of `benches/swebench/tasks.json`); selected for axis coverage across `traceback / named_api / symptom_only` × `sparse / medium / dense` × `1 / 2-3 / 4+` |
| Variants | 15: 8 strategies × `auto` direction + (v2/v3a/v3b) × {forward, both, reverse_only} |
| Gold fix-sites | All 20 verified by [`verify_fix_sites.py`](../bench/reverse_expand_panel/verify_fix_sites.py) — every modified line in the gold patch is covered by the declared fix-site set. Initial auto-extraction had 60% wrong (missing class prefix etc.); all corrected. |
| `strongest_anchor` / `density` / `hops` | Hand-curated against warm workspace via `anchors / impact / flow` (see runbook §2b) |
| Forward-reach metric | Mirrors `fix_site_in_impact` for callee direction — uses `flow strongest_anchor fix_site` (variant-invariant per task) |
| Result file | `target/reverse_expand_panel/20260429-1629-20task.jsonl` (300 rows) |

## What this doesn't measure

Per the design spec — restating because it bears on how the recommendation should be applied:

- **Agent behavior**: the agent never runs. A variant that "lifts retrieval" may or may not let the agent succeed downstream. Triangulate with `benches/swebench/run.py` before merging any default change.
- **Cold-start cost**: the panel runs on warm workspaces. First-run capsule generation may amortize differently.
- **Cell coverage gaps**: 16 of 27 cells in the design grid are empty. The recommendation may not generalize to bug shapes those cells represent.
- **The `4+ hops` cell is structurally absent** from the panel — the static flow walker either resolves a path within depth 3 or returns no path (graph indirection); neither produces a `4+` bucket. If shipped engine-side support exists for chains beyond depth 3, the panel will need a different way to identify those tasks.
