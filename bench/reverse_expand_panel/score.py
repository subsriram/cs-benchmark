#!/usr/bin/env python3
"""Score one (variant, task) result.

Pure function: takes the parsed `--json` outputs of
`codesurgeon context` and `codesurgeon impact`, plus the task's gold
fix sites, and returns the metrics defined in
`docs/reverse_expand_panel.md`.

No subprocess, no I/O — kept pure so it can be unit-tested cheaply.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable


@dataclass
class Metrics:
    fix_site_in_pivots: bool
    fix_site_in_skeletons: bool
    fix_site_in_impact: bool
    fix_site_in_forward_reach: bool  # gold reachable from strongest_anchor via `flow` (callee direction); mirrors fix_site_in_impact for the forward direction (codesurgeon#96)
    fix_site_rank: int | None       # 1-indexed position in capsule.pivots ++ capsule.skeletons
    matched_fix_site: str | None    # which gold FQN landed (or None)
    pivot_count: int
    capsule_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_fqns(entries: Iterable[dict]) -> list[str]:
    return [e["fqn"] for e in entries]


def _fqn_match(observed: str, gold: str) -> bool:
    """Lenient match.

    Gold FQNs are extracted from gold patches as `path::Class::method` or
    `path::function`. Observed FQNs come from codesurgeon's index, which
    uses the same shape — but not always the same separator depth (e.g.
    Python module paths). Treat as a match if both end in the same
    `Class::method` or `function` *and* share the file path prefix.
    """
    if observed == gold:
        return True
    # Class-pivot ⊃ method-fix-site: a pivot of `path::Class` returns the
    # full class source body, which contains every method including
    # `path::Class::method`. The agent reading the capsule sees the
    # buggy method body. astropy-7166 hit this case — fix-site
    # `InheritDocstrings::__init__` is "in" the class pivot.
    if gold.startswith(observed + "::"):
        return True
    # File path prefix + symbol-name suffix match
    if "::" in gold and "::" in observed:
        gold_path, _, gold_sym = gold.partition("::")
        obs_path, _, obs_sym = observed.partition("::")
        if gold_path == obs_path and gold_sym.endswith(gold.split("::", 1)[-1].split("::")[-1]):
            return obs_sym.endswith(gold_sym.split("::")[-1])
    # Last-segment match (function name only) — fallback when classes
    # disagree but the function name is unique enough. Caller decides if
    # this is acceptable; the metric records `matched_fix_site` so the
    # match can be audited.
    return observed.rsplit("::", 1)[-1] == gold.rsplit("::", 1)[-1]


def score(
    capsule: dict,
    impact: dict | None,
    gold_fix_sites: list[str],
    forward_reach: bool = False,
) -> Metrics:
    pivots = capsule.get("pivots", []) or []
    skeletons = capsule.get("skeletons", []) or []
    stats = capsule.get("stats", {}) or {}

    pivot_fqns = _extract_fqns(pivots)
    skeleton_fqns = _extract_fqns(skeletons)
    impact_fqns: list[str] = []
    if impact is not None:
        impact_fqns = (
            _extract_fqns(impact.get("direct_dependents", []) or [])
            + _extract_fqns(impact.get("transitive_dependents", []) or [])
        )

    fix_in_pivots = False
    fix_in_skeletons = False
    fix_in_impact = False
    rank: int | None = None
    matched: str | None = None
    for gold in gold_fix_sites:
        for i, fqn in enumerate(pivot_fqns):
            if _fqn_match(fqn, gold):
                fix_in_pivots = True
                if rank is None:
                    rank = i + 1
                matched = gold
        for i, fqn in enumerate(skeleton_fqns):
            if _fqn_match(fqn, gold):
                fix_in_skeletons = True
                if rank is None:
                    rank = len(pivot_fqns) + i + 1
                if matched is None:
                    matched = gold
        for fqn in impact_fqns:
            if _fqn_match(fqn, gold):
                fix_in_impact = True
                if matched is None:
                    matched = gold

    return Metrics(
        fix_site_in_pivots=fix_in_pivots,
        fix_site_in_skeletons=fix_in_skeletons,
        fix_site_in_impact=fix_in_impact,
        fix_site_in_forward_reach=forward_reach,
        fix_site_rank=rank,
        matched_fix_site=matched,
        pivot_count=int(stats.get("pivot_count", len(pivot_fqns))),
        capsule_tokens=int(stats.get("total_tokens", 0)),
    )
