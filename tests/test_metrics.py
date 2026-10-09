"""Metrics member: test retries, negative savings, units and precision."""

import unittest

from backend import metrics


class ClassifierOverheadTests(unittest.TestCase):
    """PROPOSAL: the optional classifier= keyword is purely additive."""

    ATTEMPTS = [{"tier": "small"}]

    def test_default_output_is_identical_to_before(self):
        self.assertEqual(
            metrics.calculate_metrics(self.ATTEMPTS),
            metrics.calculate_metrics(self.ATTEMPTS, classifier=None),
        )

    def test_no_extra_keys_when_the_classifier_did_not_run(self):
        for classifier in (None, {"used": False, "status": "skipped"}):
            with self.subTest(classifier=classifier):
                result = metrics.calculate_metrics(self.ATTEMPTS, classifier=classifier)
                self.assertEqual(sorted(result), ["baseline", "impact", "savings"])

    def test_overhead_appears_only_when_a_call_was_attempted(self):
        result = metrics.calculate_metrics(self.ATTEMPTS, classifier={"used": True})
        self.assertIn("classifier_overhead", result)
        self.assertIn("impact_including_classifier", result)

    def test_existing_values_are_never_altered(self):
        before = metrics.calculate_metrics(self.ATTEMPTS)
        after = metrics.calculate_metrics(self.ATTEMPTS, classifier={"used": True})
        for section in ("impact", "baseline", "savings"):
            self.assertEqual(before[section], after[section], section)

    def test_overhead_is_one_small_tier_call_using_contract_constants(self):
        overhead = metrics.calculate_metrics(
            self.ATTEMPTS, classifier={"used": True})["classifier_overhead"]
        small = metrics.TIER_ESTIMATES["small"]
        self.assertEqual(overhead["energy_wh"], small["energy_wh"])
        self.assertEqual(overhead["cost_inr"], small["cost_inr"])
        self.assertAlmostEqual(overhead["co2_g"], small["energy_wh"] * metrics.CO2_G_PER_WH)
        self.assertAlmostEqual(overhead["water_ml"], small["energy_wh"] * metrics.WATER_ML_PER_WH)
        self.assertTrue(overhead["estimated"], "must be labelled an estimate")

    def test_impact_including_classifier_is_impact_plus_overhead(self):
        result = metrics.calculate_metrics(
            [{"tier": "medium"}], classifier={"used": True})
        for key in metrics.METRIC_KEYS:
            self.assertAlmostEqual(
                result["impact_including_classifier"][key],
                result["impact"][key] + result["classifier_overhead"][key], msg=key)

    def test_a_failed_classifier_call_is_still_charged(self):
        # "used" means a call was ATTEMPTED - the same rule attempts follow.
        result = metrics.calculate_metrics(
            self.ATTEMPTS, classifier={"used": True, "status": "timeout", "method": "fallback"})
        self.assertEqual(result["classifier_overhead"]["energy_wh"],
                         metrics.TIER_ESTIMATES["small"]["energy_wh"])

    def test_helper_returns_none_when_there_was_no_call(self):
        self.assertIsNone(metrics.classifier_overhead(None))
        self.assertIsNone(metrics.classifier_overhead({"used": False}))


if __name__ == "__main__":
    unittest.main()
