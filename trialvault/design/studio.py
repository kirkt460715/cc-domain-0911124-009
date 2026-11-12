from ..validation import (
    name,
    object_value,
    integer,
    decimal,
    decimal_text,
    canonical,
    digest,
    parse_json,
    reject,
)
from .spec import DesignSpec, validate_sources, seed_text
from .estimate import estimate_components
from .scenarios import primary_scenarios, sensitivity_scenarios
from .runtime import (
    simulate_replicate,
    summarize,
    check_replicates,
    replicate_digest,
)


class DesignStudio:
    def __init__(self, database, registry, ingestion, artifacts):
        self.db = database
        self.registry = registry
        self.ingestion = ingestion
        self.artifacts = artifacts
        with database.transaction() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS design_specs(id TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(id,version))"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS design_runs(id TEXT PRIMARY KEY,request TEXT NOT NULL,result TEXT NOT NULL,digest TEXT NOT NULL)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS design_comparisons(id TEXT PRIMARY KEY,request TEXT NOT NULL,result TEXT NOT NULL,digest TEXT NOT NULL)"
            )

    def define(self, design):
        object_value(design)
        spec = DesignSpec.parse(design)
        source = validate_sources(spec, self.registry)
        payload = {"spec": spec.as_dict(), "source": source}
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT digest FROM design_specs WHERE id=? AND version=?",
                (spec.id, spec.version),
            )
            if old:
                if old["digest"] != digest(payload):
                    reject("conflict", "design version is immutable")
                return {"design": spec.summary(), "existing": True}
            latest = self.db.one(
                "SELECT version FROM design_specs WHERE id=? ORDER BY version DESC LIMIT 1",
                (spec.id,),
            )
            if latest:
                if spec.version != latest["version"] + 1:
                    reject("design", "design versions must be consecutive")
            elif spec.version != 1:
                reject("design", "first design version must be one")
            con.execute(
                "INSERT INTO design_specs VALUES(?,?,?,?)",
                (spec.id, spec.version, canonical(payload), digest(payload)),
            )
            self.db.audit(
                "design_defined",
                spec.id,
                {
                    "version": spec.version,
                    "digest": spec.fingerprint,
                    "variance_source": source,
                },
            )
        return {
            "design": spec.summary(),
            "source": source,
            "existing": False,
        }

    def stored(self, design_id, version):
        row = self.db.one(
            "SELECT * FROM design_specs WHERE id=? AND version=?",
            (design_id, version),
        )
        if row is None:
            reject("missing", "design version not found")
        payload = parse_json(row["payload"])
        if digest(payload) != row["digest"]:
            reject("integrity", "design payload digest mismatch")
        spec = DesignSpec.parse(payload["spec"])
        return spec, payload["source"], payload

    def get(self, design_id, version):
        spec, source, _ = self.stored(design_id, version)
        return {
            "design": spec.as_dict(),
            "summary": spec.summary(),
            "source": source,
            "validity": self._source_validity(source, spec),
        }

    def list(self, design_id=None):
        if design_id is None:
            rows = self.db.rows("SELECT id,version FROM design_specs ORDER BY id,version")
        else:
            name(design_id)
            rows = self.db.rows(
                "SELECT id,version FROM design_specs WHERE id=? ORDER BY version",
                (design_id,),
            )
        result = []
        for row in rows:
            spec, source, _ = self.stored(row["id"], row["version"])
            result.append(
                {**spec.summary(), "validity": self._source_validity(source, spec)}
            )
        return result

    def _source_validity(self, source, spec):
        problems = []
        variance = spec.variance
        if variance["kind"] == "historical":
            try:
                record = self.registry.version(variance["dataset"], variance["version"])
            except Exception as exc:
                from ..validation import VaultError

                if not isinstance(exc, VaultError):
                    raise
                problems.append(
                    {
                        "code": "missing_source",
                        "dataset": variance["dataset"],
                        "version": variance["version"],
                        "message": exc.code,
                    }
                )
            else:
                if record["state"] != "active":
                    problems.append(
                        {
                            "code": "withdrawn_source",
                            "dataset": variance["dataset"],
                            "version": variance["version"],
                        }
                    )
                if source and record["digest"] != source.get("digest"):
                    problems.append(
                        {
                            "code": "source_changed",
                            "dataset": variance["dataset"],
                            "version": variance["version"],
                        }
                    )
        return {"valid": not problems, "planning_invalid": bool(problems), "problems": problems}

    def components(self, spec):
        """Resolve variance components from either assumptions or a historical version."""
        variance = spec.variance
        if variance["kind"] == "assumptions":
            return {
                "kind": "assumptions",
                "between_batch_variance": variance["between_batch_variance"],
                "within_batch_variance": variance["within_batch_variance"],
                "source": variance["source"],
                "note": variance["note"],
                "icc": _icc_value(
                    variance["between_batch_variance"], variance["within_batch_variance"]
                ),
                "warnings": [],
            }
        record = self.registry.version(variance["dataset"], variance["version"])
        schema = self.registry.schema(
            self.registry.dataset(variance["dataset"])["schema_id"],
            record["schema_version"],
        )
        rows = self.ingestion.rows(variance["dataset"], variance["version"])
        estimate = estimate_components(
            rows=rows,
            value_field=variance["value_field"],
            batch_field=variance["batch_field"],
            group_field=variance.get("group_field"),
        )
        estimate["dataset"] = variance["dataset"]
        estimate["version"] = variance["version"]
        estimate["version_digest"] = record["digest"]
        estimate["source"] = "historical"
        estimate["note"] = variance.get("note")
        return estimate

    def variance(self, design_id, version):
        spec, source, _ = self.stored(design_id, version)
        validity = self._source_validity(source, spec)
        components = self.components(spec)
        return {
            "design": {"id": design_id, "version": version},
            "components": components,
            "validity": validity,
            "stored_source": source,
            "meaningful_difference": {
                "value": decimal_text(spec.mmd),
                "source": spec.mmd_source,
                "note": spec.mmd_note,
                "derived_from_history": False,
            },
        }

    def _scenarios(self, spec, components, options):
        primary = primary_scenarios(spec, components, options)
        cells = sensitivity_scenarios(spec, components, options)
        return primary, cells

    def plan(
        self,
        run_id,
        design_id,
        design_version,
        seed,
        replicates=200,
        budget=None,
        effects=None,
        sensitivity=None,
    ):
        name(run_id)
        spec, source, payload = self.stored(design_id, design_version)
        validity = self._source_validity(source, spec)
        if not validity["valid"]:
            reject(
                "state",
                "design input version is no longer valid",
                problems=validity["problems"],
            )
        seed = seed_text(seed)
        replicates = check_replicates(replicates, 2, 2000)
        if budget is None:
            reject("design", "fixed total budget is required")
        budget_value = decimal(budget)
        if budget_value <= 0:
            reject("design", "budget must be positive")
        cost = decimal(spec.planned_cost())
        if cost > budget_value:
            reject(
                "budget",
                "design cost exceeds the fixed budget",
                cost=spec.planned_cost(),
                budget=decimal_text(budget_value),
            )
        options = _options(effects, sensitivity)
        from .scenarios import validate_effects

        validate_effects(spec, options.get("effects"))
        components = self.components(spec)
        primary, cells = self._scenarios(spec, components, options)
        scenarios = [primary["null"], primary["alternative"]] + cells
        cell_keys = [item["key"] for item in scenarios]
        if len(set(cell_keys)) != len(cell_keys):
            reject("design", "duplicate sensitivity scenario keys")
        scenario_count = len(scenarios)
        if replicates * scenario_count > 20000:
            reject(
                "design",
                "simulation budget exceeds replicate limit",
                replicates=replicates,
                scenarios=scenario_count,
            )
        request = {
            "run": run_id,
            "design": {"id": design_id, "version": design_version, "digest": spec.fingerprint},
            "seed": seed,
            "replicates": replicates,
            "budget": decimal_text(budget_value),
            "cost": spec.planned_cost(),
            "components": _component_snapshot(components),
            "options": options,
            "scenarios": scenarios,
            "source": source,
        }
        result = self._execute(spec, request, components, primary, cells)
        with self.db.transaction() as con:
            old = self.db.one("SELECT * FROM design_runs WHERE id=?", (run_id,))
            if old:
                if old["request"] != canonical(request):
                    reject("conflict", "design run identity differs")
                return self.run(run_id)
            con.execute(
                "INSERT INTO design_runs VALUES(?,?,?,?)",
                (
                    run_id,
                    canonical(request),
                    canonical(result),
                    digest(result),
                ),
            )
            self.db.audit(
                "design_planned_simulation",
                run_id,
                {
                    "design": design_id,
                    "version": design_version,
                    "seed": seed,
                    "replicates": replicates,
                },
            )
        return self.run(run_id)

    def _execute(self, spec, request, components, primary, cells):
        summaries = {}
        replicate_log = []
        scenario_defs = [primary["null"], primary["alternative"]] + cells
        for scenario in scenario_defs:
            replicates = []
            for index in range(request["replicates"]):
                replicate = simulate_replicate(
                    spec, scenario, request["seed"], index
                )
                replicate["digest"] = replicate_digest(replicate)
                replicates.append(replicate)
                replicate_log.append(
                    {
                        "scenario": scenario["key"],
                        "index": index,
                        "seed": replicate["seed"],
                        "status": replicate["status"],
                        "digest": replicate["digest"],
                    }
                )
            summaries[scenario["key"]] = summarize(replicates, spec, scenario)
        cost = decimal(spec.planned_cost())
        budget = decimal(request["budget"])
        return {
            "run": request["run"],
            "design": request["design"],
            "seed": request["seed"],
            "replicates": request["replicates"],
            "cost": spec.planned_cost(),
            "budget": request["budget"],
            "within_budget": cost <= budget,
            "remaining_budget": _text(budget - cost),
            "planned": {
                "batches": len(spec.batch_sizes),
                "specimens": sum(spec.batch_sizes),
                "allocation_unit": spec.allocation_unit,
                "block_size": spec.block_size,
            },
            "components": _component_snapshot(components),
            "variance_estimate": _variance_label(components),
            "primary": {
                "null": summaries[primary["null"]["key"]],
                "alternative": summaries[primary["alternative"]["key"]],
            },
            "type_one": {
                "target_alpha": float(spec.alpha),
                "by_contrast": [
                    {
                        "group": item["group"],
                        "estimate": item["power_among_attempts"]["estimate"],
                        "interval": item["power_among_attempts"]["wilson_interval"],
                        "bonferroni": item["power_at_bonferroni"],
                    }
                    for item in summaries[primary["null"]["key"]]["contrasts"]
                ],
                "any_group": summaries[primary["null"]["key"]]["any_group_power"],
                "any_group_bonferroni": summaries[primary["null"]["key"]][
                    "any_group_power_bonferroni"
                ],
            },
            "sensitivity": [summaries[cell["key"]] for cell in cells],
            "simulation": {
                "scenarios": len(scenario_defs),
                "attempted_replicates": len(replicate_log),
                "replicate_log_digest": digest(replicate_log),
                "replicate_log": replicate_log,
                "notes": [
                    "replicates that fail or cannot be estimated remain counted",
                    "no future trial outcome is read by this planning step",
                    "no formal efficacy conclusion is produced",
                ],
            },
            "analysis": _analysis_label(spec),
            "meaningful_difference": {
                "value": decimal_text(spec.mmd),
                "source": spec.mmd_source,
                "note": spec.mmd_note,
                "is_design_input": True,
                "derived_from_history": False,
            },
            "sources": request["source"],
        }

    def run(self, run_id):
        row = self.db.one("SELECT * FROM design_runs WHERE id=?", (run_id,))
        if row is None:
            reject("missing", "design run not found")
        result = parse_json(row["result"])
        if digest(result) != row["digest"]:
            reject("integrity", "design run result digest mismatch")
        validity = self._run_validity(parse_json(row["request"]))
        return {**result, "validity": validity}

    def _run_validity(self, request):
        problems = []
        try:
            spec, source, _ = self.stored(
                request["design"]["id"], request["design"]["version"]
            )
        except Exception as exc:
            from ..validation import VaultError

            if not isinstance(exc, VaultError):
                raise
            problems.append({"code": "missing_design", "message": exc.code})
        else:
            if spec.fingerprint != request["design"]["digest"]:
                problems.append({"code": "design_changed"})
            problems.extend(self._source_validity(source, spec)["problems"])
        return {
            "valid": not problems,
            "planning_invalid": bool(problems),
            "problems": problems,
        }

    def runs(self, design_id=None):
        rows = self.db.rows(
            "SELECT id,request,digest FROM design_runs ORDER BY id"
        )
        result = []
        for row in rows:
            request = parse_json(row["request"])
            if design_id is not None and request["design"]["id"] != design_id:
                continue
            result.append(
                {
                    "run": row["id"],
                    "design": request["design"],
                    "seed": request["seed"],
                    "replicates": request["replicates"],
                    "cost": request["cost"],
                    "budget": request["budget"],
                    "validity": self._run_validity(request),
                }
            )
        return result

    def verify(self, run_id):
        """Re-run each replicate from its stored seed and compare its digest."""
        row = self.db.one("SELECT * FROM design_runs WHERE id=?", (run_id,))
        if row is None:
            reject("missing", "design run not found")
        request = parse_json(row["request"])
        stored = parse_json(row["result"])
        if digest(stored) != row["digest"]:
            reject("integrity", "design run result digest mismatch")
        spec, source, _ = self.stored(
            request["design"]["id"], request["design"]["version"]
        )
        scenarios = request["scenarios"]
        by_key = {cell["key"]: cell for cell in request["scenarios"]}
        mismatches = []
        checked = 0
        scenarios_to_redo = [
            (entry["scenario"], entry["index"])
            for entry in stored["simulation"]["replicate_log"]
        ]
        recomputed = {}
        for key in {key for key, _ in scenarios_to_redo}:
            recomputed[key] = []
        for key, index in scenarios_to_redo:
            cell = by_key.get(key)
            if cell is None:
                mismatches.append({"scenario": key, "code": "missing_scenario"})
                continue
            replicate = simulate_replicate(spec, cell, request["seed"], index)
            recomputed[key].append(replicate)

        stored_summaries = {
            "null": stored["primary"]["null"],
            "alternative": stored["primary"]["alternative"],
        }
        for item in stored.get("sensitivity", []):
            stored_summaries[item["scenario"]] = item
        for key, cell in by_key.items():
            if key not in recomputed:
                continue
            redo = summarize(
                sorted(recomputed[key], key=lambda x: x["index"]), spec, cell
            )
            if stored_summaries.get(key) != redo:
                mismatches.append({"scenario": key, "code": "summary_differs"})
        for entry in stored["simulation"]["replicate_log"]:
            cell = by_key.get(entry["scenario"])
            if cell is None:
                continue
            replicate = next(
                item
                for item in recomputed[entry["scenario"]]
                if item["index"] == entry["index"]
            )
            expected = replicate_digest(replicate)
            checked += 1
            if expected != entry["digest"]:
                mismatches.append(
                    {
                        "scenario": entry["scenario"],
                        "index": entry["index"],
                        "seed": entry["seed"],
                        "code": "replicate_differs",
                    }
                )
        return {
            "run": run_id,
            "replicates_checked": checked,
            "mismatches": mismatches,
            "reproducible": not mismatches,
            "planning_invalid": self._run_validity(request)["planning_invalid"],
        }

    def compare(
        self,
        comparison_id,
        candidates,
        seed,
        replicates=200,
        budget=None,
        effects=None,
        sensitivity=None,
    ):
        name(comparison_id)
        if not isinstance(candidates, list) or not 2 <= len(candidates) <= 12:
            reject("design", "comparison needs two to twelve candidate designs")
        if budget is None:
            reject("design", "fixed total budget is required")
        budget_value = decimal(budget)
        if budget_value <= 0:
            reject("design", "budget must be positive")
        seed = seed_text(seed)
        replicates = check_replicates(replicates, 2, 2000)
        options = _options(effects, sensitivity)
        normalized = []
        seen = set()
        for candidate in candidates:
            object_value(candidate, {"design", "version"}, {"design", "version"})
            design_id = name(candidate["design"])
            version = integer(candidate["version"], 1)
            if (design_id, version) in seen:
                reject("design", "duplicate comparison candidate")
            if (design_id, version) in seen:
                reject("design", "duplicate comparison candidate")
            seen.add((design_id, version))
            spec, source, _ = self.stored(design_id, version)
            validity = self._source_validity(source, spec)
            cost = decimal(spec.planned_cost())
            normalized.append(
                {
                    "design": spec,
                    "id": design_id,
                    "version": version,
                    "cost": cost,
                    "feasible": cost <= budget_value,
                    "validity": validity,
                }
            )
        request = {
            "comparison": comparison_id,
            "budget": decimal_text(budget_value),
            "seed": seed,
            "replicates": replicates,
            "options": options,
            "candidates": [
                {"design": item["id"], "version": item["version"]}
                for item in normalized
            ],
        }
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT * FROM design_comparisons WHERE id=?", (comparison_id,)
            )
            if old:
                if old["request"] != canonical(request):
                    reject("conflict", "comparison identity differs")
                return self.comparison(comparison_id)
            # Claim the identity before running so concurrent requests serialize,
            # and a failure leaves no half-record behind.
            con.execute(
                "INSERT INTO design_comparisons VALUES(?,?,?,?)",
                (comparison_id, canonical(request), canonical({"state": "running"}), digest({"state": "running"})),
            )

        try:
            result = self._compare_run(
                comparison_id, normalized, budget_value, seed, replicates,
                request, options, effects, sensitivity,
            )
        except BaseException:
            with self.db.transaction() as con:
                con.execute(
                    "DELETE FROM design_comparisons WHERE id=? AND result=?",
                    (comparison_id, canonical({"state": "running"})),
                )
            raise
        with self.db.transaction() as con:
            con.execute(
                "UPDATE design_comparisons SET result=?,digest=? WHERE id=?",
                (canonical(result), digest(result), comparison_id),
            )
            self.db.audit(
                "design_comparison",
                comparison_id,
                {
                    "candidates": len(normalized),
                    "evaluated": len(result["candidates"]),
                    "excluded": len(result["excluded"]),
                },
            )
        return self.comparison(comparison_id)

    def _compare_run(self, comparison_id, normalized, budget_value, seed, replicates, request, options, effects, sensitivity):
        evaluated = []
        excluded = []
        for item in normalized:
            entry = {
                "design": item["id"],
                "version": item["version"],
                "cost": decimal_text(item["cost"]),
                "eligible_at_budget": item["feasible"] and item["validity"]["valid"],
            }
            if not item["validity"]["valid"]:
                entry["excluded_reason"] = "invalid_input_version"
                entry["problems"] = item["validity"]["problems"]
                excluded.append(entry)
                continue
            if not item["feasible"]:
                entry["excluded_reason"] = "over_budget"
                excluded.append(entry)
                continue
            run_id = (comparison_id + f"-d{item['id']}-v{item['version']}")[:100]
            run = self.plan(
                run_id,
                item["id"],
                item["version"],
                seed,
                replicates,
                item["cost"],
                effects=effects,
                sensitivity=sensitivity,
            )
            alternative = run["primary"]["alternative"]
            null = run["primary"]["null"]
            cells = run["sensitivity"]
            power = alternative["any_group_power_bonferroni"]
            type_one = null["any_group_power_bonferroni"]
            cell_power = [
                cell["any_group_power_bonferroni"]["estimate"]
                for cell in cells
                if cell["any_group_power_bonferroni"]["estimate"] is not None
            ]
            robustness = min(cell_power) if cell_power else power["estimate"]
            evaluated.append(
                {
                    **entry,
                    "run": run_id,
                    "power": power,
                    "power_unadjusted": alternative["any_group_power"],
                    "type_one": type_one,
                    "type_one_unadjusted": null["any_group_power"],
                    "completed_rate": alternative["completed"]["rate"],
                    "robustness": robustness,
                    "sensitivity_power": [
                        {
                            "scenario": cell["scenario"],
                            "power": cell["any_group_power_bonferroni"]["estimate"],
                            "failed": cell["replicate_status"].get("failed_fit", 0)
                            + cell["replicate_status"].get("unevaluable", 0),
                            "completed": cell["completed"]["rate"],
                        }
                        for cell in cells
                    ],
                }
            )

        result = {
            "comparison": comparison_id,
            "budget": decimal_text(budget_value),
            "seed": seed,
            "replicates": replicates,
            "candidates": evaluated,
            "excluded": excluded,
            "frontier": _non_dominated(evaluated),
            "dimensions": ["cost_minimized", "power_maximized", "robustness_maximized"],
            "notes": [
                "power and type-one use a Bonferroni family-wise threshold across group contrasts",
                "robustness is the minimum alternative power across sensitivity cells",
                "excluded candidates remain visible with their reason",
                "this comparison is prospective planning, not an efficacy conclusion",
            ],
        }
        return result

    def comparison(self, comparison_id):
        row = self.db.one(
            "SELECT * FROM design_comparisons WHERE id=?", (comparison_id,)
        )
        if row is None:
            reject("missing", "design comparison not found")
        result = parse_json(row["result"])
        if result.get("state") == "running":
            reject("state", "design comparison is still running or failed")
        if digest(result) != row["digest"]:
            reject("integrity", "design comparison digest mismatch")
        problems = []
        for candidate in result["candidates"]:
            run = self.run(candidate["run"])
            if run["validity"]["planning_invalid"]:
                problems.append(
                    {
                        "design": candidate["design"],
                        "version": candidate["version"],
                        "problems": run["validity"]["problems"],
                    }
                )
        return {
            **result,
            "planning_invalid": bool(problems),
            "validity_problems": problems,
        }


def _options(effects, sensitivity):
    options = {}
    if effects is not None:
        object_value(effects, {"kind", "values", "scale"}, {"kind"})
        options["effects"] = effects
    if sensitivity is not None:
        object_value(sensitivity, {"icc", "dropout_multiplier"}, set())
        if "icc" not in sensitivity and "dropout_multiplier" not in sensitivity:
            reject("design", "sensitivity needs icc or dropout_multiplier")
        options["sensitivity"] = sensitivity
    return options


def _component_snapshot(components):
    result = {
        "between_batch_variance": _text(components["between_batch_variance"]),
        "within_batch_variance": _text(components["within_batch_variance"]),
        "kind": components.get("kind", "historical"),
    }
    between = decimal(result["between_batch_variance"])
    within = decimal(result["within_batch_variance"])
    total = between + within
    result["icc"] = float(between / total) if total > 0 else 0.0
    if components.get("warnings"):
        result["warnings"] = components["warnings"]
    if components.get("source") is not None:
        result["source"] = components["source"]
    return result


def _variance_label(components):
    if components.get("kind") == "assumptions":
        return {
            "kind": "assumptions",
            "source": components.get("source"),
            "note": components.get("note"),
            "icc": components.get("icc"),
        }
    return {
        "kind": "historical",
        "dataset": components.get("dataset"),
        "version": components.get("version"),
        "version_digest": components.get("version_digest"),
        "estimator": components.get("estimator"),
        "between_batch_variance": components.get("between_batch_variance"),
        "within_batch_variance": components.get("within_batch_variance"),
        "icc": components.get("icc"),
        "warnings": components.get("warnings", []),
    }


def _analysis_label(spec):
    if spec.allocation_unit == "batch":
        return {
            "unit": "batch",
            "test": "welch_t_on_batch_means",
            "note": "treating one batch as one independent randomized unit",
        }
    return {
        "unit": "specimen",
        "test": "stratified_t_within_batch_blocks",
        "note": "randomization is within batch; batch effects cancel in the stratified contrast",
    }


def _text(value):
    return format(decimal(value), "f")


def _icc_value(between_text, within_text):
    between = decimal(between_text)
    within = decimal(within_text)
    total = between + within
    if total <= 0:
        reject("design", "variance assumptions have zero total")
    return float(between / total)


def _non_dominated(candidates):
    frontier = []
    for candidate in candidates:
        dominated = False
        for other in candidates:
            if other is candidate:
                continue
            if _dominates(other, candidate):
                dominated = True
                break
        if not dominated:
            frontier.append(
                {
                    "design": candidate["design"],
                    "version": candidate["version"],
                    "cost": candidate["cost"],
                    "power": candidate["power"]["estimate"],
                    "robustness": candidate["robustness"],
                    "run": candidate["run"],
                }
            )
    return frontier


def _dominates(left, right):
    left_cost = decimal(left["cost"])
    right_cost = decimal(right["cost"])
    left_power = left["power"]["estimate"]
    right_power = right["power"]["estimate"]
    left_robust = left["robustness"]
    right_robust = right["robustness"]
    if left_power is None or right_power is None:
        return False
    if left_robust is None or right_robust is None:
        return False
    no_worse = (
        left_cost <= right_cost
        and left_power >= right_power
        and left_robust >= right_robust
    )
    better = (
        left_cost < right_cost
        or left_power > right_power
        or left_robust > right_robust
    )
    return no_worse and better
