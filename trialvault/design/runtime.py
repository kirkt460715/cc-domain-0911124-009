"""Deterministic Monte Carlo execution for prospective power planning.

Every replicate is derived from a seed material and a replicate index, so the
same configuration and seed reproduce each round independently. Replicates that
cannot be fitted are kept in the accounting rather than silently discarded.
"""
import math
import random
from ..validation import digest, integer, reject
from .beta import t_cdf

REASONS = {
    "insufficient_batches": "fewer than two batches contribute to a contrast",
    "insufficient_group_batches": "a treatment group has fewer than two batches",
    "zero_variance": "estimated standard error is zero",
    "nonfinite": "estimate or standard error is not finite",
}


def _replicate_record(replicate):
    return {
        "scenario": replicate["scenario"],
        "index": replicate["index"],
        "seed": replicate["seed"],
        "status": replicate["status"],
        "planned_batches": replicate["planned_batches"],
        "planned_specimens": replicate["planned_specimens"],
        "completed_batches": replicate["completed_batches"],
        "completed_specimens": replicate["completed_specimens"],
        "missing_batches": replicate["missing_batches"],
        "missing_specimens": replicate["missing_specimens"],
        "contrasts": replicate["contrasts"],
    }


def replicate_digest(replicate):
    return digest(_replicate_record(replicate))


def scenario_seed(master, design_digest, scenario_key, index):
    return int.from_bytes(
        digest(
            {
                "master": master,
                "design": design_digest,
                "scenario": scenario_key,
                "replicate": index,
            }
        ).encode(),
        "big",
    ) % (2**63)


def _stratified_t(groups, group, reference):
    """Stratified (batch-paired) t-test for specimen allocation.

    Randomization is within each batch, so the batch effect cancels in the
    within-batch group difference. Specimens are not independent across the
    trial, so the standard error comes from the pooled within-batch variation
    and the degrees of freedom are within-batch, not total specimen count.
    """
    if group not in groups or reference not in groups:
        return None, "insufficient_batches"
    shared = sorted(set(groups[group]) & set(groups[reference]))
    if len(shared) < 2:
        return None, "insufficient_batches"
    differences = []
    weights = []
    residual_ss = 0.0
    specimens = 0
    for batch in shared:
        left = groups[group][batch]
        right = groups[reference][batch]
        n_left, n_right = len(left), len(right)
        mean_left = sum(left) / n_left
        mean_right = sum(right) / n_right
        differences.append(mean_left - mean_right)
        weights.append(n_left * n_right / (n_left + n_right))
        residual_ss += sum((value - mean_left) ** 2 for value in left)
        residual_ss += sum((value - mean_right) ** 2 for value in right)
        specimens += n_left + n_right
    df = specimens - 2 * len(shared)
    if df <= 0:
        return None, "insufficient_batches"
    within_variance = residual_ss / df
    if not math.isfinite(within_variance):
        return None, "nonfinite"
    if within_variance <= 0:
        return None, "zero_variance"
    weight_sum = sum(weights)
    if weight_sum <= 0:
        return None, "zero_variance"
    estimate = sum(w * d for w, d in zip(weights, differences)) / weight_sum
    se_squared = within_variance / weight_sum
    if not math.isfinite(se_squared) or se_squared <= 0:
        return None, "nonfinite"
    se = math.sqrt(se_squared)
    t_stat = estimate / se
    p = 2.0 * (1.0 - t_cdf(abs(t_stat), df))
    return {
        "estimate": estimate,
        "standard_error": se,
        "p": min(1.0, max(0.0, p)),
        "degrees_of_freedom": df,
        "batches": {"group": len(groups[group]), "reference": len(groups[reference])},
    }, None


def _welch(groups, group, reference):
    """Welch t-test on batch means; one batch is one independent unit."""
    left = [sum(values) / len(values) for values in groups[group].values()]
    right = [sum(values) / len(values) for values in groups[reference].values()]
    n_left, n_right = len(left), len(right)
    if n_left < 2 or n_right < 2:
        return None, "insufficient_group_batches"
    mean_left = sum(left) / n_left
    mean_right = sum(right) / n_right
    var_left = sum((value - mean_left) ** 2 for value in left) / (n_left - 1)
    var_right = sum((value - mean_right) ** 2 for value in right) / (n_right - 1)
    component_left = var_left / n_left
    component_right = var_right / n_right
    denominator = component_left + component_right
    if not math.isfinite(denominator):
        return None, "nonfinite"
    if denominator <= 0:
        return None, "zero_variance"
    difference = mean_left - mean_right
    t_stat = difference / math.sqrt(denominator)
    df = denominator**2 / (
        component_left**2 / (n_left - 1) + component_right**2 / (n_right - 1)
    )
    if not math.isfinite(df) or df <= 0:
        return None, "nonfinite"
    p = 2.0 * (1.0 - t_cdf(abs(t_stat), df))
    return {
        "estimate": difference,
        "standard_error": math.sqrt(denominator),
        "p": min(1.0, max(0.0, p)),
        "batches": {"group": n_left, "reference": n_right},
    }, None


def allocation_labels(size, groups, block_size, rng, balanced_remainder=True):
    """Randomized complete blocks; a remainder block is balanced when requested."""
    labels = []
    block = list(groups) * (block_size // len(groups))
    remaining = size
    while remaining >= block_size:
        shuffled = block[:]
        rng.shuffle(shuffled)
        labels.extend(shuffled)
        remaining -= block_size
    if balanced_remainder:
        while remaining > 0:
            shuffled = list(groups)
            rng.shuffle(shuffled)
            take = min(remaining, len(groups))
            labels.extend(shuffled[:take])
            remaining -= take
    return labels


def batch_allocation(batch_sizes, groups, rng):
    """Assign whole batches to groups in blocks containing every group."""
    labels = allocation_labels(
        len(batch_sizes), groups, len(groups), rng, balanced_remainder=False
    )

    def build():
        result = {group: [] for group in groups}
        for size, label in zip(batch_sizes, labels):
            result[label].append(size)
        return result

    for _ in range(500):
        result = build()
        if all(result[group] for group in groups):
            return result
    reject(
        "design",
        "batch allocation cannot give every treatment group a batch",
        batches=len(batch_sizes),
        groups=len(groups),
    )


def simulate_replicate(spec, scenario, master_seed, index):
    """Generate one randomized trial and analyse it with the matching test."""
    seed = scenario_seed(master_seed, spec.fingerprint, scenario["key"], index)
    rng = random.Random(seed)
    between = scenario["between_batch_variance"]
    within = scenario["within_batch_variance"]
    batch_sd = math.sqrt(between)
    within_sd = math.sqrt(within)
    truth = scenario["group_effects"]
    drop = scenario["dropout"]
    batch_rate, specimen_rate = drop["batch_rate"], drop["specimen_rate"]
    groups = spec.groups
    sizes = spec.batch_sizes
    records = []
    total_specimens = 0
    total_batches = 0
    missing_batches = 0
    missing_specimens = 0
    if spec.allocation_unit == "batch":
        assigned = batch_allocation(sizes, groups, rng)
        ordered = []
        for group in groups:
            ordered.extend((group, size) for size in assigned[group])
        rng.shuffle(ordered)
        plan = [(group, size) for group, size in ordered]
    else:
        plan = [
            (allocation_labels(size, groups, spec.block_size, rng), size)
            for size in sizes
        ]
    for unit in plan:
        if spec.allocation_unit == "batch":
            group, size = unit
            labels = [group] * size
        else:
            labels, size = unit
        batch_effect = rng.gauss(0.0, batch_sd) if batch_sd > 0 else 0.0
        total_batches += 1
        batch_dropped = batch_rate > 0 and rng.random() < batch_rate
        if batch_dropped:
            missing_batches += 1
            total_specimens += size
            continue
        for label in labels:
            total_specimens += 1
            if specimen_rate > 0 and rng.random() < specimen_rate:
                missing_specimens += 1
                continue
            value = truth[label] + batch_effect + rng.gauss(0.0, within_sd)
            records.append(
                {
                    "batch": total_batches,
                    "group": label,
                    "value": value,
                }
            )
    completed_specimens = len(records)
    completed_batches = len({row["batch"] for row in records})
    contrasts = _analyse(spec, records)
    reasons = [
        item.get("reason") for item in contrasts.values() if item["status"] == "failed"
    ]
    evaluated = [item for item in contrasts.values() if item["status"] == "evaluated"]
    if evaluated:
        status = "evaluated"
    elif any(reason in {"zero_variance", "nonfinite"} for reason in reasons):
        status = "failed_fit"
    else:
        status = "unevaluable"
    return {
        "index": index,
        "seed": seed,
        "scenario": scenario["key"],
        "status": status,
        "planned_batches": total_batches,
        "planned_specimens": total_specimens,
        "completed_batches": completed_batches,
        "completed_specimens": completed_specimens,
        "missing_batches": missing_batches,
        "missing_specimens": missing_specimens,
        "contrasts": contrasts,
    }


def _analyse(spec, records):
    groups = {}
    for row in records:
        groups.setdefault(row["group"], {}).setdefault(row["batch"], []).append(
            row["value"]
        )
    result = {}
    for group in spec.groups:
        if group == spec.reference:
            continue
        if group not in groups:
            result[group] = {"status": "failed", "reason": "insufficient_batches"}
            continue
        if spec.allocation_unit == "specimen":
            if spec.reference not in groups:
                result[group] = {
                    "status": "failed",
                    "reason": "insufficient_batches",
                }
                continue
            estimate, reason = _stratified_t(groups, group, spec.reference)
        else:
            if spec.reference not in groups:
                result[group] = {
                    "status": "failed",
                    "reason": "insufficient_group_batches",
                }
                continue
            estimate, reason = _welch(groups, group, spec.reference)
        if estimate is None:
            result[group] = {"status": "failed", "reason": reason}
        else:
            result[group] = {"status": "evaluated", **estimate}
    return result


def summarize(replicates, spec, scenario):
    """Aggregate without discarding failed or unevaluable replicates."""
    alpha = float(spec.alpha)
    attempts = len(replicates)
    status_counts = {"evaluated": 0, "failed_fit": 0, "unevaluable": 0}
    completed_specimens = 0
    completed_batches = 0
    missing_batches = 0
    missing_specimens = 0
    contrasts = {}
    for group in spec.groups:
        if group != spec.reference:
            contrasts[group] = {
                "attempts": 0,
                "evaluable": 0,
                "failed": 0,
                "significant": 0,
                "significant_unadjusted_any": 0,
                "significant_bonferroni": 0,
                "failure_reasons": {},
            }
    any_unadjusted = 0
    any_bonferroni = 0
    comparisons = len(contrasts) or 1
    adjusted = alpha / comparisons
    for replicate in replicates:
        status = replicate["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        completed_specimens += replicate["completed_specimens"]
        completed_batches += replicate["completed_batches"]
        missing_batches += replicate["missing_batches"]
        missing_specimens += replicate["missing_specimens"]
        evaluated_any = False
        significant_any = False
        significant_adjusted_any = False
        for group, item in contrasts.items():
            item["attempts"] += 1
            contrast = replicate["contrasts"].get(
                group, {"status": "failed", "reason": "insufficient_batches"}
            )
            if contrast["status"] == "evaluated":
                item["evaluable"] += 1
                evaluated_any = True
                if contrast["p"] < alpha:
                    item["significant"] += 1
                    significant_any = True
                if contrast["p"] < adjusted:
                    item["significant_bonferroni"] += 1
                    significant_adjusted_any = True
            else:
                item["failed"] += 1
                reason = contrast.get("reason", "nonfinite")
                item["failure_reasons"][reason] = (
                    item["failure_reasons"].get(reason, 0) + 1
                )
        if significant_any:
            any_unadjusted += 1
        if significant_adjusted_any:
            any_bonferroni += 1
    def proportion(successes, denominator):
        if denominator <= 0:
            return {
                "estimate": None,
                "numerator": successes,
                "denominator": denominator,
                "mc_standard_error": None,
                "wilson_interval": None,
            }
        p = successes / denominator
        return {
            "estimate": p,
            "numerator": successes,
            "denominator": denominator,
            "mc_standard_error": math.sqrt(p * (1 - p) / denominator),
            "wilson_interval": wilson_interval(successes, denominator),
        }

    detailed = []
    for group, item in contrasts.items():
        detailed.append(
            {
                "group": group,
                "reference": spec.reference,
                "attempts": item["attempts"],
                "evaluable": item["evaluable"],
                "failed": item["failed"],
                "failure_reasons": dict(sorted(item["failure_reasons"].items())),
                "power_among_attempts": proportion(item["significant"], item["attempts"]),
                "power_among_evaluable": proportion(
                    item["significant"], item["evaluable"]
                ),
                "alpha": alpha,
                "alpha_bonferroni": adjusted,
                "power_at_bonferroni": proportion(
                    item["significant_bonferroni"], item["attempts"]
                ),
            }
        )
    completion = proportion(status_counts.get("evaluated", 0), attempts)
    return {
        "scenario": scenario["key"],
        "truth": scenario["label"],
        "effect_per_group": scenario["effect"],
        "alpha": alpha,
        "attempts": attempts,
        "replicate_status": status_counts,
        "completed": {
            "replicates": status_counts.get("evaluated", 0),
            "rate": completion,
            "specimens_per_replicate": _average(completed_specimens, attempts),
            "batches_per_replicate": _average(completed_batches, attempts),
        },
        "loss": {
            "mechanism": scenario["dropout"]["mechanism"],
            "missing_batches_per_replicate": _average(missing_batches, attempts),
            "missing_specimens_per_replicate": _average(missing_specimens, attempts),
        },
        "any_group_power": proportion(any_unadjusted, attempts),
        "any_group_power_bonferroni": proportion(any_bonferroni, attempts),
        "contrasts": detailed,
        "aim": scenario["aim"],
    }


def _average(total, count):
    return total / count if count else 0.0


def wilson_interval(successes, total, z=1.959963984540054):
    if total <= 0:
        return None
    p = successes / total
    denominator = 1.0 + z * z / total
    center = (p + z * z / (2.0 * total)) / denominator
    half = (
        z
        * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total))
        / denominator
    )
    return [max(0.0, center - half), min(1.0, center + half)]


def check_replicates(value, minimum=1, maximum=5000):
    return integer(value, minimum, maximum)
