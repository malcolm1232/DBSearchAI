"""Count-based cluster regression gate (spec 2026-07-31 sections 3 and 6, review C3).
Percentages are not gated at slice level; slices are small, so arithmetic is counts of
newly failed variant clusters versus the baseline of the SAME key.

Two independent bounds, both counted from the same current-vs-baseline cluster diff:
a per-slice bound (`max_lost_per_slice`) and a per-capability aggregate bound
(`max_lost_per_capability`) that dedupes a cluster across every slice of its
capability before comparing (a cluster failing in 3 slices of one capability counts
ONCE at the capability level). Red iff either bound is exceeded anywhere."""
from __future__ import annotations

from dataclasses import dataclass


def cluster_of(item) -> str:
    return item.variant_of or item.id


def slice_rows(rows: list) -> dict:
    out: dict = {}
    for row in rows:
        for tag in row["hardness"]:
            out.setdefault(f"{row['capability']}|{tag}|{row['mode']}", []).append(row)
    return out


def _failed_clusters(rows: list) -> set:
    return {r["cluster"] for r in rows if not r["passed"]}


def _capability_of(key: str) -> str:
    return key.split("|", 1)[0]


def _cluster_attribution(rows: list, cluster: str) -> str:
    """The attribution of the first (by id) CURRENT row that both belongs to
    `cluster` and is itself failing. Used only to annotate a regression message;
    never changes red/green (MINOR-6)."""
    failing = sorted((r for r in rows if r["cluster"] == cluster and not r["passed"]),
                     key=lambda r: r["id"])
    return failing[0]["attribution"] if failing else "unknown"


def _annotate(rows: list, clusters) -> list:
    return [f"{c} ({_cluster_attribution(rows, c)})" for c in sorted(clusters)]


def check_keys(current: dict, baseline: dict) -> None:
    diff = [k for k in sorted(set(current) | set(baseline))
            if current.get(k) != baseline.get(k)]
    if diff:
        raise ValueError(f"baseline key mismatch on {diff}: comparison across "
                         f"profiles/models/packs is invalid (spec section 3)")


@dataclass(frozen=True)
class GateResult:
    red: bool
    regressions: list


def compare(current_rows: list, baseline_rows: list, max_lost_per_slice: int = 1,
            max_lost_per_capability: int = 2) -> GateResult:
    """Count-based cluster regression gate (spec section 6).

    A slice regresses when it newly fails more than `max_lost_per_slice` clusters
    versus the baseline of the SAME key. A capability regresses when the clusters
    newly failed across ALL of its slices, deduplicated (a cluster failing in 3
    slices of one capability counts ONCE at the capability level), exceed
    `max_lost_per_capability`. Red iff any slice OR any capability exceeds its
    bound; each regression message names which bound tripped and annotates every
    newly-failed cluster with its current failure attribution (MINOR-6)."""
    cur, base = slice_rows(current_rows), slice_rows(baseline_rows)
    regressions = []
    capability_newly: dict = {}
    for key in sorted(cur):
        newly = _failed_clusters(cur[key]) - _failed_clusters(base.get(key, []))
        capability_newly.setdefault(_capability_of(key), set()).update(newly)
        if len(newly) > max_lost_per_slice:
            regressions.append(
                f"{key}: newly failed clusters {_annotate(cur[key], newly)} "
                f"(slice bound {max_lost_per_slice} exceeded)")
    for cap in sorted(capability_newly):
        newly = capability_newly[cap]
        if len(newly) > max_lost_per_capability:
            cap_rows = [r for r in current_rows if r["capability"] == cap]
            regressions.append(
                f"capability {cap}: newly failed clusters {_annotate(cap_rows, newly)} "
                f"(capability bound {max_lost_per_capability} exceeded)")
    return GateResult(red=bool(regressions), regressions=regressions)
