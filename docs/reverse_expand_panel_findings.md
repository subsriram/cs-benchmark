# Reverse-Expand Diagnostic Panel — current findings

**Status:** snapshot as of `codesurgeon 1.0.0 (sha 0d5e627a8956, built 2026-04-30T00:55:31Z)` against the 20-task / 11-cell panel. Most-recent run id: `20260429-2059-per-seed-rrf` (post-#96 per-seed RRF list split). Recommendation unchanged from prior snapshot (`3e6c379b57f8` — depth-stratified RRF).

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

## Engine work history + what would change the picture

| Engine sha | Engine work | Panel result |
|---|---|---|
| `3e6c379b57f8` | Depth-stratified RRF (`EXPAND_DEEP_RRF_K=8` for depth ≥ 2) | All v3-family variants tied; chain in emissions but not pivots |
| **`0d5e627a8956` (current)** | Per-seed RRF list split — each seed gets its own deep list at k=8, no cross-seed competition in fusion | **No headline flip.** v3 variants gain ~5-10pp on skeletons but lose ~5-15pp on pivots (chain demoted from pivot to skeleton on a few cells). matplotlib-24177 + django-16938 (the predicted forward-flip cases) stay False on `fix_site_in_pivots`. |

Per-seed RRF was the right step for the cross-seed-dilution bottleneck (verified — `pyplot::hist` correctly contributes 0 deep emissions to its own list, no longer dilutes `Axes::hist`'s chain). The remaining bottleneck is **intra-seed depth dilution**: even within `Axes::hist`'s own deep list, the priority queue at depth-2 favors siblings over `add_patch`'s children, so `_update_patch_limits` doesn't make it into the depth-3 emissions despite the walker reaching that depth.

### Next engine work — would unstick more cells

The bottleneck on `0d5e627a8956` is the priority signal inside `expand_best_first`. Two non-mutually-exclusive ideas surfaced in the [#96 thread](https://github.com/subsriram/codesurgeon/issues/96#issuecomment-4348890291):

1. **Depth-continuation bonus**: when popping a node at depth N whose parent was popped at high priority, give the children a small priority bonus so chain depth doesn't compete with breadth at the same depth from a different parent.
2. **Per-tree-node UCB**: track popped/unpopped within each parent's children separately, so unpopped children of high-priority parents get an exploration bonus.

If either lands and unsticks matplotlib-24177 (the cleanest probe — 4-hop chain, anchor extraction correct, priority is the only thing in the way), the recommendation may flip:
- v3 family (with the `auto` classifier fixed) becomes a real default candidate
- v0/v1/v2 still strict regressions; deprecate from variants.toml
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
| Result file (current) | `target/reverse_expand_panel/20260429-2059-per-seed-rrf.jsonl` (300 rows, sha `0d5e627a8956`) |
| Result file (prior — depth-stratified RRF) | `target/reverse_expand_panel/20260429-1629-20task.jsonl` (300 rows, sha `3e6c379b57f8`) |

## Agent-loop corroboration on sympy-21379

A single agent-loop run on the canonical adversarial fixture corroborates the panel-level recommendation. Run setup: `codesurgeon ba72ca19e9c6` (post-#101 graph fixes, post-#126 schema fix), **strategy = `none`** (the only one shipped in this binary; the experimental v0/v1*/v2/v3* variants and `CS_EXPAND_DIRECTION` axis are gated out post-#97), fresh re-index of the sympy-21379 warm workspace, `claude-opus-4-7[1m]`, $1 budget cap.

| Arm | walltime | cost | turns | output tokens | diff | succeeded? |
|---|---:|---:|---:|---:|---:|---|
| `without` (bare claude, pilot) | 96.5s | $0.297 | n/a | 4,363 | 610B | ✓ adds import + try/except, smaller scope |
| `with` (codesurgeon `none`, attempt 1) | 137.6s | $1.003 (cap) | 27 | 7,392 | 728B | ✗ correct scope, missing import → NameError at runtime |
| `with` (codesurgeon `none`, attempt 2) | 132.8s | $0.995 (cap) | 27 | 7,587 | **1,070B** | **✓ identical to gold patch** |

Three facts converge:

**1. This run is `none` — no reverse-expand walk happened.** `none` retrieval is BM25 + ANN + anchor-extraction only. The shipped binary doesn't expose the experimental v0/v1*/v2/v3* strategies, so this single agent-loop datapoint is *only* about the shipped baseline; it's not testing reverse-expand in any form.

**2. The `none` capsule for sympy-21379 doesn't contain the fix site.** Direct probe of `codesurgeon context` (strategy `none`) against the fresh sympy index, with the actual problem statement as `--context`:

```
pivots (8, 3,488/4,000 budget tokens):
  test_unexpected_exception_is_passed_through_with     (test file)
  Piecewise                                             (user-named symptom)
  DMP::all_terms                                        (irrelevant)
  Poly::all_terms                                       (irrelevant)
  test_pickling_polys_errors                            (test file)
  Subs                                                  (user-named symptom)
  BasePolynomialError                                   (parent class — not the actual exception)
  PolynomialDivisionFailed                              (wrong subclass)

Mod::eval         absent
sympy/core/mod.py absent  ← entire file containing the fix
gcd, gcd_terms    absent
```

The pivots are exactly what BM25 + ANN would surface from the problem statement: `Piecewise` and `Subs` are lexically named in the bug report; `BasePolynomialError` and `PolynomialDivisionFailed` are semantically near `PolynomialError`; the test files have words from the problem statement. None of these point at `Mod::eval`.

This is **not** the same failure mode as reverse-expand on this task. The panel showed reverse-expand variants also produced `fix_site_in_pivots: 0` on sympy-21379, but for a different reason: the static graph doesn't connect `PolynomialError` to `Mod::eval` (Mod::eval calls `gcd`, which calls things that *raise* PolynomialError — a reverse-walk from the symptom goes to *raisers*, not to *upstream callers of raisers*). Two different mechanisms, same outcome on this task: capsule misses the fix.

**3. The agent's `with`-arm "success" comes from doing the same exploratory work bare claude does.** Across 27 turns, the agent makes many file Reads regardless of whether codesurgeon is in the loop — because the `none` capsule didn't point at `mod.py`. The codesurgeon overhead is a 3,488-token irrelevant capsule that costs cache-creation/cache-read tokens to maintain across turns, with zero offsetting benefit. Cost is 3.4× higher; outcome quality is non-deterministic (one attempt produces gold, one produces broken-fix).

The panel's recommendation ("stay on `none`") is reinforced from a different angle here. The panel was about *which expand variant to ship over `none`* and concluded "none of them"; this agent-loop run is about *whether `none` itself adds enough value to justify codesurgeon's overhead on this bug shape* and concludes **no, on this task it doesn't**. That's a related but distinct claim — and it shifts the question from "tune the variants" to "tune retrieval anchoring so `none` actually finds upstream callers when the user names a symptom."

**Generalization caveat:** n=1 task, 2 attempts. Not a scaled study. The canonical adversarial fixture from the #69 thread is the right single-task probe for "where codesurgeon should plausibly earn its keep" — and on this binary's `none` it doesn't, even though the agent eventually solves the bug. A scaled `with`/`without` sweep on multiple tasks (and ideally rerun once an experimental binary with v0/v1*/v2/v3* re-exposed lands) would be needed before drawing a stronger conclusion.

## What this doesn't measure

Per the design spec — restating because it bears on how the recommendation should be applied:

- **Agent behavior at scale**: only the single sympy-21379 datapoint in the prior section actually runs the agent. A panel-wide agent-loop sweep would still be needed before shipping any default change; what's here only confirms the recommendation isn't directly contradicted by the canonical hard task.
- **Cold-start cost**: the panel runs on warm workspaces. First-run capsule generation may amortize differently.
- **Cell coverage gaps**: 16 of 27 cells in the design grid are empty. The recommendation may not generalize to bug shapes those cells represent.
- **The `4+ hops` cell is structurally absent** from the panel — the static flow walker either resolves a path within depth 3 or returns no path (graph indirection); neither produces a `4+` bucket. If shipped engine-side support exists for chains beyond depth 3, the panel will need a different way to identify those tasks.
