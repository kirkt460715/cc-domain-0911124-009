import tempfile
import unittest
from pathlib import Path
from trialvault.service import Vault
from trialvault.validation import VaultError, canonical, digest
from support import register

HISTORY = {
    "id": "history",
    "version": 1,
    "fields": [
        {"name": "specimen", "type": "text"},
        {"name": "batch", "type": "text"},
        {"name": "group", "type": "text"},
        {"name": "value", "type": "decimal"},
    ],
    "primary_key": ["specimen"],
}


def history_rows(groups=("control", "treated"), batches=8, specimens=6, between=2.5, within=1.5):
    import random

    rng = random.Random(17)
    rows = []
    for batch in range(batches):
        batch_effect = rng.gauss(0, between)
        for group, mean in zip(groups, (10.0, 13.0)):
            for specimen in range(specimens):
                rows.append(
                    {
                        "specimen": f"{group}-{batch}-{specimen}",
                        "batch": f"b{batch}",
                        "group": group,
                        "value": f"{mean + batch_effect + rng.gauss(0, within):.4f}",
                    }
                )
    return rows


def design(**changes):
    base = {
        "id": "candidate",
        "version": 1,
        "groups": ["control", "treated"],
        "reference": "control",
        "mmd": "2.0",
        "mmd_source": "clinical_relevance",
        "allocation": {"unit": "specimen", "block_size": 4},
        "batch_sizes": [8, 8, 8, 8, 8, 8],
        "cost": {"fixed": "100", "per_batch": "20", "per_specimen": "2"},
        "variance": {
            "kind": "historical",
            "dataset": "history",
            "version": 1,
            "value_field": "value",
            "batch_field": "batch",
            "group_field": "group",
        },
        "dropout": {"mechanism": "both", "batch_rate": "0.05", "specimen_rate": "0.05"},
    }
    base.update(changes)
    return base


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Vault(Path(self.temp.name) / "vault")
        self.addCleanup(self.vault.close)
        self.vault.registry.add_schema(HISTORY)
        self.vault.registry.register("history", "Historical", "history", "laboratory")
        rows = history_rows()
        content = ("\n".join(canonical(row) for row in rows) + "\n").encode()
        self.vault.ingestion.ingest(
            "hist", "history", 1, content, "jsonl", "internal", ["research"]
        )

    def define(self, **changes):
        return self.vault.designs.define(design(**changes))

    def test_design_versions_are_immutable_and_record_sources(self):
        defined = self.define()
        self.assertFalse(defined["existing"])
        self.assertEqual(defined["design"]["cost"], "316")
        self.assertEqual(defined["source"]["dataset"], "history")
        self.assertFalse(defined["source"]["digest"] == digest(""))
        self.assertTrue(self.define()["existing"])
        with self.assertRaises(VaultError):
            self.define(title="different payload with same version")
        with self.assertRaises(VaultError):
            self.define(version=3)
        self.define(version=2, batch_sizes=[12, 12, 12, 12])
        self.assertEqual(len(self.vault.designs.list("candidate")), 2)

    def test_historical_effect_cannot_become_the_meaningful_difference(self):
        for source in [None, "historical_effect", "observed_difference"]:
            with self.subTest(source=source):
                if source is None:
                    request = design()
                    del request["mmd_source"]
                else:
                    request = design(mmd_source=source)
                with self.assertRaises(VaultError):
                    self.vault.designs.define(request)
        with self.assertRaises(VaultError):
            self.define(mmd="0")
        self.define(mmd="5", mmd_source="protocol", mmd_note="fixed before data collection")

    def test_variance_estimation_uses_only_variation_not_effects(self):
        self.define()
        estimated = self.vault.designs.variance("candidate", 1)
        component = estimated["components"]
        self.assertEqual(component["kind"], "historical")
        self.assertGreater(float(component["between_batch_variance"]), 1.0)
        self.assertGreater(float(component["within_batch_variance"]), 1.0)
        self.assertNotIn("group_effect", component)
        self.assertNotIn("mean", component)
        self.assertEqual(estimated["validity"]["valid"], True)
        assumptions = {
            "kind": "assumptions",
            "between_batch_variance": "3",
            "within_batch_variance": "2",
            "source": "prior programme",
        }
        self.define(
            id="assumed",
            variance=assumptions,
        )
        estimated = self.vault.designs.variance("assumed", 1)
        self.assertEqual(estimated["components"]["kind"], "assumptions")
        self.assertEqual(estimated["components"]["source"], "prior programme")
        self.assertAlmostEqual(estimated["components"]["icc"], 0.6)

    def test_variance_source_checks_reject_unsuitable_columns(self):
        with self.assertRaises(VaultError):
            self.define(variance={**design()["variance"], "value_field": "batch"})
        with self.assertRaises(VaultError):
            self.define(variance={**design()["variance"], "version": 99})

    def test_plan_keeps_failures_visible_and_reports_mc_error(self):
        self.define()
        run = self.vault.designs.plan(
            "run", "candidate", 1, "seed-1", replicates=40, budget="1000"
        )
        alternative = run["primary"]["alternative"]
        self.assertEqual(alternative["attempts"], 40)
        counts = alternative["replicate_status"]
        self.assertEqual(sum(counts.values()), 40)
        for key in ("evaluated", "failed_fit", "unevaluable"):
            self.assertIn(key, counts)
        power = alternative["any_group_power"]
        self.assertIsNotNone(power["mc_standard_error"])
        low, high = power["wilson_interval"]
        self.assertGreaterEqual(low, 0)
        self.assertLessEqual(high, 1)
        contrast = alternative["contrasts"][0]
        self.assertEqual(
            contrast["attempts"],
            contrast["evaluable"] + contrast["failed"],
        )
        self.assertEqual(run["simulation"]["attempted_replicates"], 80)
        self.assertEqual(
            len(run["simulation"]["replicate_log"]), 80
        )
        self.assertEqual(run["cost"], "316")
        self.assertEqual(run["remaining_budget"], "684")
        self.assertTrue(run["within_budget"])
        self.assertFalse(run["meaningful_difference"]["derived_from_history"])
        self.assertEqual(run["meaningful_difference"]["source"], "clinical_relevance")
        self.assertEqual(
            run["variance_estimate"]["version_digest"],
            self.vault.registry.version("history", 1)["digest"],
        )

    def test_null_calibration_and_reproducibility(self):
        self.define(batch_sizes=[10, 10, 10, 10, 10, 10, 10, 10])
        first = self.vault.designs.plan(
            "run", "candidate", 1, "seed", replicates=400, budget="2000"
        )
        null = first["primary"]["null"]
        self.assertGreater(null["any_group_power"]["estimate"], 0.0)
        self.assertLess(null["any_group_power"]["estimate"], 0.15)
        self.assertIsNotNone(null["any_group_power"]["wilson_interval"])
        again = self.vault.designs.plan(
            "again", "candidate", 1, "seed", replicates=400, budget="2000"
        )
        self.assertEqual(first["primary"], again["primary"])
        self.assertEqual(
            first["simulation"]["replicate_log_digest"],
            again["simulation"]["replicate_log_digest"],
        )
        verified = self.vault.designs.verify("run")
        self.assertTrue(verified["reproducible"])
        self.assertEqual(verified["replicates_checked"], 800)
        with self.assertRaises(VaultError):
            self.vault.designs.plan(
                "run", "candidate", 1, "seed", replicates=10, budget="2000"
            )
        with self.assertRaises(VaultError):
            self.vault.designs.plan(
                "over", "candidate", 1, "seed", replicates=10, budget="100"
            )

    def test_whole_batch_and_single_specimen_loss_are_distinct(self):
        self.define(
            batch_sizes=[10, 10, 10, 10, 10, 10],
            dropout={"mechanism": "batch", "batch_rate": "0.5"},
        )
        run = self.vault.designs.plan(
            "batch", "candidate", 1, "seed", replicates=50, budget="1000"
        )
        alternative = run["primary"]["alternative"]
        self.assertGreater(alternative["loss"]["missing_batches_per_replicate"], 0)
        self.assertEqual(alternative["loss"]["missing_specimens_per_replicate"], 0)
        self.define(
            id="specimen",
            version=1,
            dropout={"mechanism": "specimen", "specimen_rate": "0.5"},
        )
        run = self.vault.designs.plan(
            "specimen", "specimen", 1, "seed", replicates=50, budget="1000"
        )
        alternative = run["primary"]["alternative"]
        self.assertEqual(alternative["loss"]["missing_batches_per_replicate"], 0)
        self.assertGreater(alternative["loss"]["missing_specimens_per_replicate"], 0)
        with self.assertRaises(VaultError):
            self.define(dropout={"mechanism": "batch", "specimen_rate": "0.1"})

    def test_complete_batch_loss_can_make_contrast_unestimable_without_deletion(self):
        self.define(
            batch_sizes=[6, 6, 6, 6, 6, 6],
            dropout={"mechanism": "batch", "batch_rate": "0.9"},
        )
        run = self.vault.designs.plan(
            "loss", "candidate", 1, "seed", replicates=30, budget="1000"
        )
        alternative = run["primary"]["alternative"]
        statuses = alternative["replicate_status"]
        self.assertLess(statuses["evaluated"], 30)
        self.assertGreater(statuses["unevaluable"], 0)
        self.assertEqual(alternative["attempts"], 30)
        self.assertEqual(
            alternative["contrasts"][0]["failure_reasons"].get("insufficient_batches", 0)
            > 0,
            True,
        )

    def test_withdrawal_marks_planning_invalid_but_keeps_artifacts(self):
        self.define()
        run = self.vault.designs.plan(
            "run", "candidate", 1, "seed", replicates=20, budget="1000"
        )
        self.assertFalse(run["validity"]["planning_invalid"])
        self.vault.registry.withdraw("history", 1, "Calibration fault")
        loaded = self.vault.designs.run("run")
        self.assertTrue(loaded["validity"]["planning_invalid"])
        self.assertEqual(
            [item["code"] for item in loaded["validity"]["problems"]],
            ["withdrawn_source"],
        )
        self.assertTrue(self.vault.designs.verify("run")["planning_invalid"])
        listed = self.vault.designs.list("candidate")
        self.assertTrue(listed[0]["validity"]["planning_invalid"])
        with self.assertRaises(VaultError):
            self.vault.designs.plan(
                "new", "candidate", 1, "seed", replicates=20, budget="1000"
            )
        self.assertEqual(len(self.vault.designs.runs("candidate")), 1)

    def test_sensitivity_and_frontier_are_explicit(self):
        self.define()
        self.define(
            id="dense",
            version=1,
            batch_sizes=[16, 16, 16, 16, 16, 16, 16, 16],
            cost={"fixed": "100", "per_batch": "20", "per_specimen": "2"},
        )
        self.define(
            id="over",
            version=1,
            batch_sizes=[100, 100, 100, 100],
            cost={"fixed": "100", "per_batch": "20", "per_specimen": "2"},
        )
        comparison = self.vault.designs.compare(
            "compare",
            [
                {"design": "candidate", "version": 1},
                {"design": "dense", "version": 1},
                {"design": "over", "version": 1},
            ],
            "seed",
            replicates=40,
            budget="800",
            sensitivity={"icc": ["0.2", "0.6"], "dropout_multiplier": ["1", "2"]},
        )
        self.assertEqual(len(comparison["excluded"]), 1)
        self.assertEqual(comparison["excluded"][0]["excluded_reason"], "over_budget")
        self.assertEqual(len(comparison["candidates"]), 2)
        for candidate in comparison["candidates"]:
            self.assertEqual(len(candidate["sensitivity_power"]), 4)
        frontier = comparison["frontier"]
        self.assertTrue(frontier)
        keys = {(item["design"], item["version"]) for item in frontier}
        for dominated in comparison["candidates"]:
            if (dominated["design"], dominated["version"]) not in keys:
                self.assertTrue(
                    any(
                        _dominates(dominated, item)
                        for item in comparison["candidates"]
                    )
                )
        self.assertEqual(comparison["dimensions"], ["cost_minimized", "power_maximized", "robustness_maximized"])
        loaded = self.vault.designs.comparison("compare")
        self.assertEqual(loaded["frontier"], frontier)

    def test_batch_level_allocation_uses_batch_as_randomization_unit(self):
        request = design(
            allocation={"unit": "batch"},
            batch_sizes=[10, 10, 10, 10, 10, 10],
            dropout={"mechanism": "none"},
        )
        self.vault.designs.define(request)
        run = self.vault.designs.plan(
            "batch", "candidate", 1, "seed", replicates=30, budget="1000"
        )
        self.assertEqual(run["analysis"]["unit"], "batch")
        self.assertEqual(run["analysis"]["test"], "welch_t_on_batch_means")
        with self.assertRaises(VaultError):
            self.define(
                id="too-few",
                allocation={"unit": "batch"},
                batch_sizes=[10, 10],
            )
        with self.assertRaises(VaultError):
            self.define(
                id="uneven",
                allocation={"unit": "batch", "block_size": 4},
            )

    def test_input_validation_is_strict(self):
        with self.assertRaises(VaultError):
            self.define(groups=["control", "treated", "treated"])
        with self.assertRaises(VaultError):
            self.define(reference="absent")
        with self.assertRaises(VaultError):
            self.define(allocation={"unit": "specimen", "block_size": 3})
        with self.assertRaises(VaultError):
            self.define(batch_sizes=[])
        with self.assertRaises(VaultError):
            self.define(batch_sizes=[0])
        with self.assertRaises(VaultError):
            self.define(cost={"fixed": "-1"})
        with self.assertRaises(VaultError):
            self.define(alpha="0.5")
        with self.assertRaises(VaultError):
            self.vault.designs.plan(
                "bad-effect",
                "candidate",
                1,
                "seed",
                10,
                "1000",
                effects={"kind": "custom", "values": {"treated": "0"}},
            )


def _dominates(left, right):
    from decimal import Decimal

    left_cost = Decimal(left["cost"])
    right_cost = Decimal(right["cost"])
    return (
        left_cost <= right_cost
        and left["power"]["estimate"] >= right["power"]["estimate"]
        and left["robustness"] >= right["robustness"]
        and (
            left_cost < right_cost
            or left["power"]["estimate"] > right["power"]["estimate"]
            or left["robustness"] > right["robustness"]
        )
    )
