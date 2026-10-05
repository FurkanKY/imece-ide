"""Pure metric helpers for the S1b triage evaluation harness.

Used only by tools/evaluate_decision_triage.py (and its tests). Deliberately
dependency-free (no numpy/pandas/sklearn) so the harness stays importable
offline with no SDK and fully deterministic across runs:

  - safe_ratio / percentile — safe division and linear-interpolation
    percentiles (None on empty input instead of 0 or a crash);
  - confusion / accuracy / per-class precision+recall — computed against the
    GOLD SEMANTIC label supplied by the caller, never against the
    classifier's own label set (avoids validation circularity);
  - expected_calibration_error — top-choice confidence vs correctness
    (docs/JEV-DESIGN.md's confidence is a distribution-shape statistic, not a
    probability);
  - multiclass_brier — the multiclass DISTRIBUTION Brier score
    sum_k (p_k - y_k)^2 over one-hot gold, deliberately NOT mixed with the
    top-label confidence.

Every ratio returns None (JSON null) instead of 0 when its denominator is 0.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence


def round6(value: float | int | None) -> float | int | None:
    """Round for a stable JSON report; pass None and ints through untouched."""
    if value is None or isinstance(value, int) and not isinstance(value, bool):
        return value
    return round(float(value), 6)


def safe_ratio(numerator: float, denominator: float) -> float | None:
    """numerator/denominator, or None when the denominator is 0 — safe division."""
    if not denominator:
        return None
    return float(numerator) / float(denominator)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile (numpy's 'linear' method), None on empty.

    `q` is in percent (0..100); p50 = percentile(values, 50).
    """
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (float(q) / 100.0)
    lower = int(position // 1)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def confusion(items: Sequence[Mapping[str, Any]], gold_key: str, predicted_key: str) -> dict[str, dict[str, int]]:
    """counts[gold][predicted] — only over keys present, sorted by the caller's json.dumps(sort_keys)."""
    matrix: dict[str, dict[str, int]] = {}
    for item in items:
        gold = str(item[gold_key])
        predicted = str(item[predicted_key])
        matrix.setdefault(gold, {})
        matrix[gold][predicted] = matrix[gold].get(predicted, 0) + 1
    return matrix


def accuracy(items: Sequence[Mapping[str, Any]], gold_key: str, predicted_key: str) -> tuple[float | None, int]:
    """(accuracy, denominator) — None accuracy when there are no items."""
    if not items:
        return None, 0
    correct = sum(1 for item in items if item[gold_key] == item[predicted_key])
    return safe_ratio(correct, len(items)), len(items)


def per_class_precision_recall(
    items: Sequence[Mapping[str, Any]],
    labels: Iterable[str],
    gold_key: str,
    predicted_key: str,
) -> dict[str, dict[str, Any]]:
    """Per label: support (gold count), predicted_as, tp, precision, recall.

    precision/recall are None when their denominator is 0 (safe division),
    which the report marks explicitly rather than presenting as 0.0.
    """
    stats: dict[str, dict[str, int]] = {
        label: {"support": 0, "predicted_as": 0, "true_positives": 0} for label in labels
    }
    for item in items:
        gold = str(item[gold_key])
        predicted = str(item[predicted_key])
        if gold in stats:
            stats[gold]["support"] += 1
        if predicted in stats:
            stats[predicted]["predicted_as"] += 1
        if gold == predicted and gold in stats:
            stats[gold]["true_positives"] += 1
    report: dict[str, dict[str, Any]] = {}
    for label, counts in stats.items():
        report[label] = {
            **counts,
            "precision": safe_ratio(counts["true_positives"], counts["predicted_as"]),
            "recall": safe_ratio(counts["true_positives"], counts["support"]),
        }
    return report


def expected_calibration_error(
    pairs: Sequence[tuple[float, bool]],
    *,
    bins: int = 10,
) -> dict[str, Any]:
    """ECE of top-choice CONFIDENCE vs CORRECTNESS (not p(prob)).

    `pairs` are (confidence in [0,1], predicted-correct bool) items. Equal-width
    bins over [0, 1]; empty bins are omitted from the table and contribute 0.
    Returns {"value": float|None, "bins": [...], "n": int, "bin_count": bins}.
    """
    if not pairs:
        return {"value": None, "bins": [], "n": 0, "bin_count": bins}
    bucket: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in pairs:
        index = min(bins - 1, max(0, int(float(confidence) * bins)))
        bucket[index].append((float(confidence), bool(correct)))
    table = []
    ece = 0.0
    for index, members in enumerate(bucket):
        low, high = index / bins, (index + 1) / bins
        entry: dict[str, Any] = {
            "range": [round(low, 6), round(high, 6)],
            "count": len(members),
            "mean_confidence": None,
            "accuracy": None,
        }
        if members:
            entry["mean_confidence"] = sum(c for c, _ in members) / len(members)
            entry["accuracy"] = sum(1.0 for _, ok in members if ok) / len(members)
            ece += (len(members) / len(pairs)) * abs(entry["accuracy"] - entry["mean_confidence"])
        table.append(entry)
    return {"value": ece, "bins": table, "n": len(pairs), "bin_count": bins}


def multiclass_brier(
    prob_rows: Sequence[tuple[Mapping[str, float], str]],
) -> dict[str, Any]:
    """Multiclass DISTRIBUTION Brier score: mean over items of sum_k (p_k - y_k)^2.

    Each item is (probabilities, gold_label); gold is one-hot. A gold label
    missing from the item's probability keys counts as p=0 for it (adds 1.0 —
    the distribution denied the true class entirely). This is deliberately a
    full-distribution score, NOT the top-label confidence the ECE uses.
    """
    if not prob_rows:
        return {"mean": None, "n": 0, "note": "multiclass distribution Brier: sum_k (p_k - y_k)^2 vs one-hot gold"}
    scores = []
    for probabilities, gold in prob_rows:
        total = 0.0
        labels = set(probabilities) | {gold}
        for label in labels:
            p = float(probabilities.get(label, 0.0))
            y = 1.0 if label == gold else 0.0
            total += (p - y) ** 2
        scores.append(total)
    return {
        "mean": safe_ratio(sum(scores), len(scores)),
        "n": len(scores),
        "note": "multiclass distribution Brier: sum_k (p_k - y_k)^2 vs one-hot gold",
    }


def stratified_split(
    items: Sequence[Any],
    *,
    label_key: Callable[[Any], str],
    name_key: Callable[[Any], str],
    salt: str = "s1b-v1",
) -> tuple[list[Any], list[Any]]:
    """Deterministic, seed-free stratified tune/holdout split.

    Within each stratum (label), members are ordered by sha256(salt + name) —
    stable across runs, processes and platforms (unlike Python's hash()) — and
    the first half goes to TUNE, the rest to HOLDOUT. Strata of size 1 go
    entirely to holdout. The split is disjoint by construction: the same
    sample can never appear in both halves (no tune/holdout leakage).
    """
    import hashlib

    strata: dict[str, list[Any]] = {}
    for item in items:
        strata.setdefault(label_key(item), []).append(item)
    tune: list[Any] = []
    holdout: list[Any] = []
    for label in sorted(strata):
        ordered = sorted(
            strata[label],
            key=lambda item: hashlib.sha256(f"{salt}:{name_key(item)}".encode("utf-8")).hexdigest(),
        )
        half = len(ordered) // 2
        tune.extend(ordered[:half])
        holdout.extend(ordered[half:])
    return tune, holdout
