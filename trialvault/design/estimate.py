from decimal import Decimal
from ..validation import decimal, decimal_text, reject


def _batch_groups(rows, value_field, batch_field, group_field):
    """Group observations by (optional group) and batch, preserving stable order."""
    result = {}
    for row in rows:
        batch = row.get(batch_field)
        if batch is None:
            continue
        value = row.get(value_field)
        if value is None:
            continue
        group = row.get(group_field) if group_field is not None else ""
        key = (group, batch)
        result.setdefault(group, {}).setdefault(key[1], []).append(
            float(decimal(value))
        )
    return {
        group: [
            (batch, values)
            for batch, values in sorted(batches.items(), key=lambda item: str(item[0]))
        ]
        for group, batches in result.items()
    }


def _one_way(batches):
    """Moment-based between/within variance components for one group."""
    k = len(batches)
    total = sum(len(values) for _, values in batches)
    if k < 2 or total <= k:
        return None
    grand = sum(sum(values) for _, values in batches) / total
    between_ss = sum(
        len(values) * (sum(values) / len(values) - grand) ** 2
        for _, values in batches
    )
    within_ss = sum(
        sum((value - sum(values) / len(values)) ** 2 for value in values)
        for _, values in batches
    )
    between_df = k - 1
    within_df = total - k
    ms_between = between_ss / between_df
    ms_within = within_ss / within_df
    n0 = (total - sum(len(values) ** 2 for _, values in batches) / total) / (
        k - 1
    )
    between = max(0.0, (ms_between - ms_within) / n0) if n0 > 0 else 0.0
    return {
        "batches": k,
        "specimens": total,
        "degrees_of_freedom": {"between": between_df, "within": within_df},
        "ms_between": ms_between,
        "ms_within": ms_within,
        "n0": n0,
        "between_batch_variance": between,
        "within_batch_variance": ms_within,
    }


def estimate_components(rows, value_field, batch_field, group_field=None):
    """Estimate inter-batch and intra-batch variance from a historical version.

    This function is deliberately variance-only: no treatment effect is estimated,
    so historical outcomes cannot quietly become the design's meaningful difference.
    """
    if not rows:
        reject("design", "historical variance source has no rows")
    grouped = _batch_groups(rows, value_field, batch_field, group_field)
    if not grouped:
        reject("design", "historical variance source has no usable measurements")
    groups = []
    warnings = []
    for group, batches in sorted(grouped.items(), key=lambda item: str(item[0])):
        estimate = _one_way(batches)
        label = group if group != "" else None
        if estimate is None:
            warnings.append(
                {
                    "code": "insufficient_batches",
                    "group": label,
                    "message": "group needs at least two batches and replicate specimens",
                }
            )
            continue
        estimate["group"] = label
        groups.append(estimate)
    if not groups:
        reject(
            "design",
            "historical data cannot supply variance components with at least two batches",
            warnings=warnings,
        )
    between_df = sum(item["degrees_of_freedom"]["between"] for item in groups)
    within_df = sum(item["degrees_of_freedom"]["within"] for item in groups)
    between = sum(
        item["between_batch_variance"] * item["degrees_of_freedom"]["between"]
        for item in groups
    ) / between_df
    within = sum(
        item["within_batch_variance"] * item["degrees_of_freedom"]["within"]
        for item in groups
    ) / within_df
    if between + within <= 0:
        reject("design", "historical data has zero variance")
    if len(groups) > 1:
        warnings.append(
            {
                "code": "group_variance_pooled",
                "message": "variance components pooled across historical treatment groups",
            }
        )
    if any(item["ms_between"] < item["ms_within"] for item in groups):
        warnings.append(
            {
                "code": "between_variance_truncated",
                "message": "negative moment estimate truncated at zero",
            }
        )
    icc = between / (between + within)
    return {
        "kind": "historical",
        "estimator": "moment_anova_one_way_within_groups",
        "value_field": value_field,
        "batch_field": batch_field,
        "group_field": group_field,
        "rows": len(rows),
        "batches": len({key for batches in grouped.values() for key, _ in batches}),
        "groups": [
            {
                "group": item["group"],
                "batches": item["batches"],
                "specimens": item["specimens"],
                "between_batch_variance": _round(item["between_batch_variance"]),
                "within_batch_variance": _round(item["within_batch_variance"]),
            }
            for item in groups
        ],
        "between_batch_variance": _round(between),
        "within_batch_variance": _round(within),
        "icc": _round(icc),
        "degrees_of_freedom": {"between": between_df, "within": within_df},
        "warnings": warnings,
    }


def _round(value, places=8):
    """Exact decimal text, so estimates round-trip through validation."""
    if value == 0:
        return "0"
    quantized = Decimal(str(round(float(value), places)))
    return decimal_text(quantized)
