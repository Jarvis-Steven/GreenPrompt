"""Backend member: validation, normalized errors, orchestration and provider adapter.

Everything marked "TEST FAKE" exists only inside these tests to exercise the
backend loop deterministically. None of it ships in the app: the app never
returns canned answers.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import config, metrics, model_clients, router, runtime_keys, storage, validator
from backend.config import TIERS
from backend.app import app
from backend.model_clients import ProviderError

client = TestClient(app, raise_server_exceptions=False)

GOOD_BODY = {"session_id": "s-1", "prompt": "What is 1 + 1?", "mode": "smart", "selected_model": None}
TIERS = ["small", "medium", "big"]


# ---------- TEST FAKES (not real components) ----------
def fake_rules(prompt):
    """Rules decide, so no classifier model call should happen."""
    return {"difficulty": "easy", "reason": "test: rule matched"}


def fake_rules_undecided(prompt):
    """Rules cannot decide, so smart mode must ask the model classifier."""
    return {"difficulty": None, "reason": "test: no rule matched"}


def fake_parse(prompt, raw_text):
    return {"difficulty": "easy", "reason": "test: parsed %r" % raw_text}


class FakeClassifier:
    """TEST FAKE for model_clients.classify. Records every call."""

    def __init__(self, outcome=None):
        self.outcome = outcome
        self.calls = []

    async def __call__(self, prompt, *, instructions, tier=None, timeout=None):
        self.calls.append(prompt)
        if isinstance(self.outcome, ProviderError):
            raise self.outcome
        return self.outcome or {"answer": "easy", "model_name": "test-classifier",
                                "latency_ms": 3, "input_tokens": 9, "output_tokens": 1}


def fake_choose(difficulty, mode, selected_model):
    return selected_model if mode == "pick" else {"easy": "small", "medium": "medium", "hard": "big"}[difficulty]


def fake_next(current_tier, quality_status, call_status):
    """The suggested policy from docs/team-workflow.md."""
    if quality_status != "failed" and call_status != "error":
        return None
    i = TIERS.index(current_tier)
    return TIERS[i + 1] if i + 1 < len(TIERS) else None


def fake_metrics(attempts):
    n = float(len(attempts))
    return {"impact": {"estimated": True, "energy_wh": n, "co2_g": n, "water_ml": n, "cost_inr": n},
            "baseline": {"energy_wh": 1.0, "co2_g": 1.0, "water_ml": 1.0, "cost_inr": 1.0},
            "savings": {"energy_wh": 1.0 - n, "co2_g": 1.0 - n, "water_ml": 1.0 - n, "cost_inr": 1.0 - n}}


def fake_record(record):
    return {"total_prompts": 1, "small_model_percentage": 100.0 if record["final_model"] == "small" else 0.0,
            "escalations": int(record["escalated"]),
            "cumulative_savings": {"energy_wh": 0.0, "co2_g": 0.0, "water_ml": 0.0, "cost_inr": 0.0}}


class FakeProvider:
    """TEST FAKE for model_clients.call_model. outcomes: tier -> dict or ProviderError."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    async def __call__(self, tier, prompt):
        self.calls.append(tier)
        outcome = self.outcomes[tier]
        if isinstance(outcome, ProviderError):
            raise outcome
        return {"answer": f"test-answer-from-{tier}", "model_name": f"test-{tier}", "latency_ms": 5,
                "input_tokens": 4, "output_tokens": 2}


def failure(code="TIMEOUT", retryable=True, tier="small"):
    return ProviderError(code, f"test failure on {tier}", retryable=retryable, model_name=f"test-{tier}", latency_ms=7)


def ok():
    return {}


class Harness:
    """Patches every collaborator with TEST FAKES for one test."""

    def __init__(self, provider, check=None, next_fn=fake_next, metrics_fn=fake_metrics,
                 rules=fake_rules, parse=fake_parse, classifier=None):
        self.provider = provider
        self.check = check or (lambda prompt, answer: {"status": "passed", "reason": "test: passed"})
        # classify_with_rules / classify_from_model_output are not in router.py yet,
        # so these patches create them (create=True).
        self.classifier = classifier if classifier is not None else FakeClassifier()
        self.patches = [
            patch.object(router, "classify_with_rules", rules, create=True),
            patch.object(router, "classify_from_model_output", parse, create=True),
            patch.object(router, "CLASSIFIER_INSTRUCTIONS", "TEST FAKE instructions", create=True),
            patch.object(model_clients, "classify", self.classifier),
            patch.object(router, "choose_model", fake_choose),
            patch.object(router, "next_model", next_fn),
            patch.object(validator, "check_answer", self.check),
            patch.object(metrics, "calculate_metrics", metrics_fn),
            patch.object(storage, "record_result", fake_record),
            patch.object(model_clients, "call_model", provider),
        ]

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def post(body=None, **overrides):
    payload = dict(GOOD_BODY if body is None else body)
    payload.update(overrides)
    return client.post("/chat", json=payload)


class HealthTests(unittest.TestCase):
    def test_health_reports_ok_without_secrets(self):
        with patch.dict(os.environ, {"SMALL_BASE_URL": "https://x.test/v1", "SMALL_API_KEY": "super-secret-key",
                                     "SMALL_MODEL": "m"}):
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.json()["providers"]["small"]["configured"])
        self.assertNotIn("super-secret-key", response.text)


class ValidationTests(unittest.TestCase):
    def assert_validation_error(self, response):
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"]["code"], "VALIDATION_ERROR")
        self.assertFalse(body["error"]["retryable"])
        self.assertTrue(body["request_id"])

    def test_empty_and_whitespace_prompts_rejected(self):
        self.assert_validation_error(post(prompt=""))
        self.assert_validation_error(post(prompt="   \n  "))

    def test_prompt_length_limits(self):
        self.assert_validation_error(post(prompt="x" * 2001))
        with Harness(FakeProvider({"small": ok()})):
            self.assertEqual(post(prompt="x" * 2000).status_code, 200)

    def test_prompt_is_trimmed_before_length_check(self):
        with Harness(FakeProvider({"small": ok()})):
            self.assertEqual(post(prompt="  " + "x" * 2000 + "  ").status_code, 200)

    def test_smart_mode_rejects_selected_model(self):
        self.assert_validation_error(post(selected_model="small"))

    def test_pick_mode_requires_a_valid_tier(self):
        self.assert_validation_error(post(mode="pick", selected_model=None))
        self.assert_validation_error(post(mode="pick", selected_model="huge"))

    def test_bad_mode_and_missing_session(self):
        self.assert_validation_error(post(mode="fast"))
        body = dict(GOOD_BODY)
        del body["session_id"]
        self.assert_validation_error(client.post("/chat", json=body))

    def test_non_json_body(self):
        response = client.post("/chat", content="not json", headers={"Content-Type": "application/json"})
        self.assert_validation_error(response)


class OrchestrationTests(unittest.TestCase):
    def test_single_passed_attempt_matches_contract_shape(self):
        provider = FakeProvider({"small": ok()})
        with Harness(provider):
            response = post()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for key in ("request_id", "session_id", "answer", "difficulty", "initial_model", "final_model",
                    "quality", "escalated", "attempts", "impact", "baseline", "savings", "summary"):
            self.assertIn(key, data)
        self.assertEqual(data["session_id"], "s-1")
        self.assertEqual(data["answer"], "test-answer-from-small")
        self.assertEqual((data["initial_model"], data["final_model"], data["escalated"]), ("small", "small", False))
        self.assertEqual(data["quality"], {"status": "passed", "reason": "test: passed"})
        self.assertEqual(data["attempts"], [{
            "tier": "small", "model_name": "test-small", "status": "success", "latency_ms": 5,
            "input_tokens": 4, "output_tokens": 2, "quality_status": "passed", "quality_reason": "test: passed"}])
        self.assertTrue(data["impact"]["estimated"])
        self.assertEqual(provider.calls, ["small"])
        self.assertNotIn("X-GreenPrompt-Dev-Stubs", response.headers)

    def test_failed_check_escalates_one_tier_at_a_time(self):
        def check(prompt, answer):
            return {"status": "failed" if "small" in answer else "passed", "reason": "test"}
        provider = FakeProvider({"small": ok(), "medium": ok()})
        with Harness(provider, check=check):
            data = post().json()
        self.assertEqual(provider.calls, ["small", "medium"])
        self.assertEqual((data["initial_model"], data["final_model"], data["escalated"]), ("small", "medium", True))
        self.assertEqual(data["answer"], "test-answer-from-medium")
        self.assertEqual([a["quality_status"] for a in data["attempts"]], ["failed", "passed"])

    def test_provider_error_is_recorded_and_escalates(self):
        provider = FakeProvider({"small": failure("RATE_LIMITED"), "medium": ok()})
        with Harness(provider):
            data = post().json()
        first = data["attempts"][0]
        self.assertEqual((first["status"], first["quality_status"]), ("error", "unchecked"))
        self.assertIn("RATE_LIMITED", first["quality_reason"])
        self.assertIsNone(first["input_tokens"])
        self.assertEqual((data["final_model"], data["escalated"]), ("medium", True))

    def test_every_tier_failing_check_stops_after_big_and_keeps_failed_status(self):
        provider = FakeProvider({"small": ok(), "medium": ok(), "big": ok()})
        with Harness(provider, check=lambda p, a: {"status": "failed", "reason": "test: wrong"}):
            data = post().json()
        self.assertEqual(provider.calls, ["small", "medium", "big"])
        self.assertEqual(data["final_model"], "big")
        self.assertEqual(data["quality"]["status"], "failed")
        self.assertEqual(len(data["attempts"]), 3)

    def test_all_calls_failing_returns_error_envelope_without_an_answer(self):
        provider = FakeProvider({t: failure("TIMEOUT", True, t) for t in TIERS})
        with Harness(provider):
            response = post()
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error"]["code"], "MODEL_UNAVAILABLE")
        self.assertTrue(body["error"]["retryable"])
        self.assertNotIn("answer", body)
        self.assertTrue(body["request_id"])

    def test_all_failures_non_retryable_when_configuration_is_missing(self):
        provider = FakeProvider({t: failure("CONFIG_MISSING", False, t) for t in TIERS})
        with Harness(provider):
            body = post().json()
        self.assertFalse(body["error"]["retryable"])

    def test_unchecked_answer_stops_without_escalation(self):
        provider = FakeProvider({"small": ok()})
        with Harness(provider, check=lambda p, a: {"status": "unchecked", "reason": "no supported check"}):
            data = post().json()
        self.assertEqual(provider.calls, ["small"])
        self.assertEqual(data["quality"]["status"], "unchecked")

    def test_a_tier_is_never_repeated_and_never_downgraded(self):
        provider = FakeProvider({"small": ok(), "medium": ok(), "big": ok()})
        with Harness(provider, check=lambda p, a: {"status": "failed", "reason": "x"},
                     next_fn=lambda tier, q, c: "small"):
            data = post().json()
        self.assertEqual(provider.calls, ["small"])
        self.assertEqual(len(data["attempts"]), 1)

    def test_pick_mode_starts_at_the_selected_tier(self):
        provider = FakeProvider({"big": ok()})
        with Harness(provider):
            data = post(mode="pick", selected_model="big").json()
        self.assertEqual(provider.calls, ["big"])
        self.assertEqual((data["initial_model"], data["final_model"]), ("big", "big"))

    def test_final_model_is_the_producer_of_the_returned_answer_even_if_later_attempt_fails(self):
        provider = FakeProvider({"small": ok(), "medium": failure("TIMEOUT", True, "medium")})
        with Harness(provider, check=lambda p, a: {"status": "failed", "reason": "test: wrong"}):
            # next_fn escalates after the failed check; the medium call then errors,
            # and the router tries big, which is not configured in the fake provider.
            provider.outcomes["big"] = failure("TIMEOUT", True, "big")
            data = post().json()
        self.assertEqual(data["final_model"], "small")
        self.assertEqual(data["answer"], "test-answer-from-small")
        self.assertEqual(data["quality"]["status"], "failed")
        self.assertTrue(data["escalated"])
        self.assertEqual([a["status"] for a in data["attempts"]], ["success", "error", "error"])

    def test_invalid_validator_status_is_treated_as_unchecked(self):
        with Harness(FakeProvider({"small": ok()}), check=lambda p, a: {"status": "great", "reason": ""}):
            data = post().json()
        self.assertEqual(data["quality"]["status"], "unchecked")

    def test_metrics_receive_every_attempt_including_errors(self):
        seen = []

        def spy(attempts):
            seen.append(attempts)
            return fake_metrics(attempts)
        provider = FakeProvider({"small": failure("TIMEOUT"), "medium": ok()})
        with Harness(provider, metrics_fn=spy):
            post()
        self.assertEqual([a["status"] for a in seen[0]], ["error", "success"])

    def test_malformed_metrics_output_returns_internal_error(self):
        with Harness(FakeProvider({"small": ok()}), metrics_fn=lambda attempts: {"impact": {}}):
            response = post()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "INTERNAL_ERROR")


class PendingComponentTests(unittest.TestCase):
    """Behaviour while teammates' functions still raise NotImplementedError."""

    @staticmethod
    def pending(*args, **kwargs):
        raise NotImplementedError

    def pending_patches(self):
        return [
            patch.object(router, "classify_with_rules", self.pending, create=True),
            patch.object(router, "classify_from_model_output", self.pending, create=True),
            patch.object(router, "choose_model", self.pending),
            patch.object(router, "next_model", self.pending),
            patch.object(validator, "check_answer", self.pending),
            patch.object(metrics, "calculate_metrics", self.pending),
            patch.object(storage, "record_result", self.pending),
        ]

    def run_with_pending(self, provider, env):
        patches = self.pending_patches() + [patch.object(model_clients, "call_model", provider),
                                            patch.dict(os.environ, env)]
        for p in patches:
            p.start()
        try:
            return post()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_returns_501_without_calling_a_provider_when_stubs_are_off(self):
        provider = FakeProvider({"small": ok()})
        response = self.run_with_pending(provider, {"GREENPROMPT_DEV_STUBS": "0"})
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.json()["error"]["code"], "NOT_IMPLEMENTED")
        self.assertEqual(provider.calls, [])

    def test_dev_stubs_are_labelled_and_never_claim_a_real_check(self):
        provider = FakeProvider({"small": ok()})
        response = self.run_with_pending(provider, {"GREENPROMPT_DEV_STUBS": "1"})
        self.assertEqual(response.status_code, 200)
        header = response.headers["X-GreenPrompt-Dev-Stubs"]
        for name in ("router.classify_with_rules", "validator.check_answer", "metrics.calculate_metrics",
                     "storage.record_result"):
            self.assertIn(name, header)
        data = response.json()
        self.assertEqual(data["quality"]["status"], "unchecked")
        self.assertIn("DEV STUB", data["quality"]["reason"])


class ClassifierTests(unittest.TestCase):
    """Hybrid difficulty classification: rules first, one model call only if needed."""

    def test_rules_decide_so_no_classifier_call_is_made(self):
        classifier = FakeClassifier()
        with Harness(FakeProvider({"small": ok()}), classifier=classifier):
            data = post().json()
        self.assertEqual(classifier.calls, [])
        self.assertEqual(data["difficulty"], "easy")
        self.assertEqual(data["classifier"]["used"], False)
        self.assertEqual(data["classifier"]["status"], "skipped")
        self.assertEqual(data["classifier"]["method"], "rules")
        self.assertIsNone(data["classifier"]["model_name"])
        self.assertEqual(data["classifier"]["difficulty"], "easy")

    def test_rules_undecided_calls_the_model_classifier_and_parses_it(self):
        classifier = FakeClassifier()
        with Harness(FakeProvider({"small": ok()}), rules=fake_rules_undecided, classifier=classifier):
            data = post().json()
        self.assertEqual(classifier.calls, ["What is 1 + 1?"])
        record = data["classifier"]
        self.assertEqual((record["used"], record["status"], record["method"]), (True, "success", "model"))
        self.assertEqual(record["model_name"], "test-classifier")
        self.assertEqual((record["latency_ms"], record["input_tokens"], record["output_tokens"]), (3, 9, 1))
        self.assertEqual(data["difficulty"], "easy")

    def test_classifier_timeout_falls_back_to_medium_and_keeps_usage_details(self):
        timeout = ProviderError("TIMEOUT", "classifier timed out", retryable=True,
                                model_name="test-classifier", latency_ms=5000)
        with Harness(FakeProvider({"medium": ok()}), rules=fake_rules_undecided,
                     classifier=FakeClassifier(timeout)):
            data = post().json()
        record = data["classifier"]
        self.assertEqual((record["used"], record["status"], record["method"]), (True, "timeout", "fallback"))
        self.assertEqual(data["difficulty"], "medium")
        # The failed call's usage must survive, not be erased.
        self.assertEqual(record["model_name"], "test-classifier")
        self.assertEqual(record["latency_ms"], 5000)
        self.assertIn("TIMEOUT", record["reason"])

    def test_provider_error_other_than_timeout_is_status_error(self):
        broken = ProviderError("RATE_LIMITED", "classifier rate limited", retryable=True,
                               model_name="test-classifier", latency_ms=11)
        with Harness(FakeProvider({"medium": ok()}), rules=fake_rules_undecided,
                     classifier=FakeClassifier(broken)):
            record = post().json()["classifier"]
        self.assertEqual((record["status"], record["method"]), ("error", "fallback"))
        self.assertEqual(record["latency_ms"], 11)

    def test_unparsable_output_is_invalid_output_and_keeps_usage(self):
        def bad_parse(prompt, raw_text):
            raise ValueError("no difficulty word found")
        with Harness(FakeProvider({"medium": ok()}), rules=fake_rules_undecided, parse=bad_parse):
            data = post().json()
        record = data["classifier"]
        self.assertEqual((record["used"], record["status"], record["method"]),
                         (True, "invalid_output", "fallback"))
        self.assertEqual(data["difficulty"], "medium")
        self.assertEqual(record["model_name"], "test-classifier")
        self.assertEqual((record["latency_ms"], record["input_tokens"], record["output_tokens"]), (3, 9, 1))

    def test_a_parser_returning_nonsense_is_also_invalid_output(self):
        with Harness(FakeProvider({"medium": ok()}), rules=fake_rules_undecided,
                     parse=lambda prompt, raw: {"difficulty": "extremely-hard", "reason": "x"}):
            record = post().json()["classifier"]
        self.assertEqual(record["status"], "invalid_output")

    def test_pick_mode_never_calls_the_classifier_and_keeps_the_chosen_tier(self):
        classifier = FakeClassifier()
        provider = FakeProvider({"big": ok()})
        with Harness(provider, rules=fake_rules_undecided, classifier=classifier):
            data = post(mode="pick", selected_model="big").json()
        self.assertEqual(classifier.calls, [])
        self.assertEqual(provider.calls, ["big"])
        self.assertEqual((data["initial_model"], data["final_model"]), ("big", "big"))
        self.assertEqual(data["classifier"]["used"], False)
        self.assertEqual(data["classifier"]["status"], "skipped")

    def test_pick_mode_with_a_rules_difficulty_reports_method_rules(self):
        classifier = FakeClassifier()
        with Harness(FakeProvider({"big": ok()}), classifier=classifier):
            record = post(mode="pick", selected_model="big").json()["classifier"]
        self.assertEqual(classifier.calls, [])
        self.assertEqual((record["method"], record["status"]), ("rules", "skipped"))

    def test_classifier_record_never_appears_inside_attempts(self):
        with Harness(FakeProvider({"small": ok()}), rules=fake_rules_undecided):
            data = post().json()
        self.assertIn("classifier", data)
        self.assertEqual(len(data["attempts"]), 1)
        for attempt in data["attempts"]:
            for forbidden in ("used", "method", "difficulty", "reason"):
                self.assertNotIn(forbidden, attempt)
            self.assertNotEqual(attempt["model_name"], "test-classifier")

    def test_classifier_is_not_passed_to_metrics_as_an_attempt(self):
        seen = []

        def spy(attempts):
            seen.append(attempts)
            return fake_metrics(attempts)
        with Harness(FakeProvider({"small": ok()}), rules=fake_rules_undecided, metrics_fn=spy):
            post()
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(seen[0][0]["model_name"], "test-small")

    def test_escalation_and_answering_counts_are_unchanged_by_classification(self):
        """Same provider outcomes, two different classification paths, same answer side."""
        def run(rules):
            provider = FakeProvider({"small": failure("TIMEOUT"), "medium": ok()})
            with Harness(provider, rules=rules):
                data = post().json()
            return provider.calls, data

        rules_calls, rules_data = run(fake_rules)
        model_calls, model_data = run(fake_rules_undecided)

        self.assertEqual(rules_calls, model_calls)
        for key in ("initial_model", "final_model", "escalated"):
            self.assertEqual(rules_data[key], model_data[key], key)
        self.assertEqual([a["status"] for a in rules_data["attempts"]],
                         [a["status"] for a in model_data["attempts"]])
        self.assertEqual(rules_data["summary"]["escalations"], model_data["summary"]["escalations"])
        self.assertTrue(rules_data["escalated"])
        # Only the classification bookkeeping differs.
        self.assertEqual(rules_data["classifier"]["method"], "rules")
        self.assertEqual(model_data["classifier"]["method"], "model")


def fake_metrics_with_classifier(attempts, *, classifier=None):
    """TEST FIXTURE: a metrics implementation that accepts the proposed keyword."""
    base = fake_metrics(attempts)
    if classifier and classifier.get("used"):
        base["classifier_overhead"] = {
            "estimated": True, "energy_wh": 0.03, "co2_g": 0.02181,
            "water_ml": 0.03, "cost_inr": 0.002}
        base["impact_including_classifier"] = {
            "estimated": True, "energy_wh": 9.9, "co2_g": 9.9,
            "water_ml": 9.9, "cost_inr": 9.9}
    return base


class ClassifierOverheadResponseTests(unittest.TestCase):
    """PROPOSAL: the overhead fields appear only when the classifier really ran."""

    def test_fields_present_when_the_classifier_made_a_call(self):
        with Harness(FakeProvider({"small": ok()}), rules=fake_rules_undecided,
                     metrics_fn=fake_metrics_with_classifier):
            data = post().json()
        self.assertTrue(data["classifier"]["used"])
        self.assertEqual(data["classifier_overhead"]["energy_wh"], 0.03)
        self.assertEqual(data["impact_including_classifier"]["energy_wh"], 9.9)

    def test_fields_absent_when_the_rules_decided(self):
        with Harness(FakeProvider({"small": ok()}), rules=fake_rules,
                     metrics_fn=fake_metrics_with_classifier):
            data = post().json()
        self.assertFalse(data["classifier"]["used"])
        self.assertIsNone(data["classifier_overhead"])
        self.assertIsNone(data["impact_including_classifier"])

    def test_existing_metric_fields_are_untouched_by_the_addition(self):
        with Harness(FakeProvider({"small": ok()}), rules=fake_rules_undecided,
                     metrics_fn=fake_metrics_with_classifier):
            data = post().json()
        plain = fake_metrics([{"tier": "small"}])
        self.assertEqual(data["impact"]["energy_wh"], plain["impact"]["energy_wh"])
        self.assertEqual(data["baseline"], plain["baseline"])

    def test_a_metrics_implementation_without_the_keyword_still_works(self):
        # fake_metrics takes only (attempts); the backend must not pass the kwarg.
        with Harness(FakeProvider({"small": ok()}), rules=fake_rules_undecided,
                     metrics_fn=fake_metrics):
            response = post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["classifier_overhead"])


class PickModeDifficultyTests(unittest.TestCase):
    """PROPOSAL (Fix C): pick mode reports a real baseline difficulty, not a default."""

    def baseline(self, value):
        return patch.object(router, "classify_prompt", lambda prompt: value, create=True)

    def test_pick_uses_the_local_baseline_and_makes_no_classifier_call(self):
        classifier = FakeClassifier()
        with self.baseline("hard"):
            with Harness(FakeProvider({"big": ok()}), rules=fake_rules_undecided,
                         classifier=classifier):
                data = post(mode="pick", selected_model="big").json()
        self.assertEqual(classifier.calls, [], "pick must never call the model classifier")
        self.assertEqual(data["difficulty"], "hard")
        self.assertEqual(data["classifier"]["method"], "rules")
        self.assertEqual(data["classifier"]["status"], "skipped")
        self.assertFalse(data["classifier"]["used"])

    def test_pick_never_overrides_the_chosen_tier(self):
        provider = FakeProvider({"small": ok()})
        with self.baseline("hard"):
            with Harness(provider, rules=fake_rules_undecided):
                data = post(mode="pick", selected_model="small").json()
        self.assertEqual(provider.calls, ["small"])
        self.assertEqual((data["initial_model"], data["final_model"]), ("small", "small"))

    def test_an_unusable_baseline_falls_back_without_crashing(self):
        with self.baseline("enormous"):
            with Harness(FakeProvider({"big": ok()}), rules=fake_rules_undecided):
                data = post(mode="pick", selected_model="big").json()
        self.assertEqual(data["difficulty"], "medium")
        self.assertEqual(data["classifier"]["method"], "fallback")


class ClassifierConfigTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        for key in ("CLASSIFIER_TIER", "CLASSIFIER_TIMEOUT_SECONDS"):
            os.environ.pop(key, None)
        self.assertEqual(config.classifier_tier(), "small")
        self.assertEqual(config.classifier_timeout_seconds(), 5.0)
        with patch.dict(os.environ, {"CLASSIFIER_TIER": "BIG", "CLASSIFIER_TIMEOUT_SECONDS": "2.5"}):
            self.assertEqual(config.classifier_tier(), "big")
            self.assertEqual(config.classifier_timeout_seconds(), 2.5)

    def test_nonsense_values_fall_back_to_defaults(self):
        with patch.dict(os.environ, {"CLASSIFIER_TIER": "enormous", "CLASSIFIER_TIMEOUT_SECONDS": "soon"}):
            self.assertEqual(config.classifier_tier(), "small")
            self.assertEqual(config.classifier_timeout_seconds(), 5.0)
        with patch.dict(os.environ, {"CLASSIFIER_TIMEOUT_SECONDS": "0"}):
            self.assertEqual(config.classifier_timeout_seconds(), 5.0)


# ---------- PROPOSED: bring your own key ----------
# FAKE_KEY is a test fixture. It is not a credential and reaches no provider.
FAKE_KEY = "fake-test-key-abcdefghijklmnop1234"


class RuntimeKeyStoreTests(unittest.TestCase):
    """The store must never expose a raw key, by any route."""

    def setUp(self):
        runtime_keys.clear_all()
        self.addCleanup(runtime_keys.clear_all)

    def test_set_returns_only_the_masked_tail(self):
        masked = runtime_keys.set_key("openai", FAKE_KEY)
        self.assertEqual(masked, "..." + FAKE_KEY[-4:])
        self.assertNotIn(FAKE_KEY, masked)

    def test_repr_and_str_never_contain_the_key(self):
        runtime_keys.set_key("openai", FAKE_KEY)
        store = runtime_keys._store
        for rendering in (repr(store), str(store), f"{store}"):
            self.assertNotIn(FAKE_KEY, rendering)
            self.assertIn("never shown", rendering)

    def test_status_never_contains_the_key(self):
        runtime_keys.set_key("gemini", FAKE_KEY)
        self.assertNotIn(FAKE_KEY, json.dumps(runtime_keys.status()))

    def test_validation_errors_never_quote_the_key(self):
        for bad in ("zqx1", FAKE_KEY + " zzz", "q" * 9999):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(runtime_keys.InvalidKey) as ctx:
                    runtime_keys.set_key("openai", bad)
                self.assertNotIn(bad, str(ctx.exception))

    def test_unknown_provider_rejected(self):
        with self.assertRaises(runtime_keys.InvalidKey):
            runtime_keys.set_key("not-a-provider", FAKE_KEY)

    def test_clear_removes_it(self):
        runtime_keys.set_key("anthropic", FAKE_KEY)
        self.assertTrue(runtime_keys.has_key("anthropic"))
        self.assertTrue(runtime_keys.clear_key("anthropic"))
        self.assertFalse(runtime_keys.has_key("anthropic"))
        self.assertEqual(runtime_keys.get_key("anthropic"), "")

    def test_tier_provider_mapping_is_the_agreed_one(self):
        self.assertEqual(runtime_keys.TIER_PROVIDER,
                         {"small": "gemini", "medium": "openai", "big": "anthropic"})


class KeyPrecedenceTests(unittest.TestCase):
    """env trio > user key > demo (Groq)."""

    def setUp(self):
        runtime_keys.clear_all()
        self.addCleanup(runtime_keys.clear_all)
        cleared = {f"{t}_{s}": "" for t in ("SMALL", "MEDIUM", "BIG")
                   for s in ("BASE_URL", "API_KEY", "MODEL")}
        self.enterContext(patch.dict(os.environ, cleared))

    def test_user_key_selects_that_providers_catalog_row(self):
        runtime_keys.set_key("gemini", FAKE_KEY)
        cfg = config.get_tier_config("small")
        self.assertEqual(cfg.model, "gemini-3.5-flash-lite")
        self.assertIn("generativelanguage", cfg.base_url)
        self.assertEqual(config.tier_source("small"), "your key")

    def test_each_tier_maps_to_its_own_provider(self):
        runtime_keys.set_key("openai", FAKE_KEY)
        runtime_keys.set_key("anthropic", FAKE_KEY)
        self.assertEqual(config.get_tier_config("medium").model, "gpt-6.1-sol")
        self.assertEqual(config.get_tier_config("big").model, "claude-opus-5-5")

    def test_explicit_env_trio_still_wins(self):
        runtime_keys.set_key("gemini", FAKE_KEY)
        override = {"SMALL_BASE_URL": "https://override.test/v1",
                    "SMALL_API_KEY": "operator-key", "SMALL_MODEL": "override-model"}
        with patch.dict(os.environ, override):
            self.assertEqual(config.get_tier_config("small").model, "override-model")
            self.assertEqual(config.tier_source("small"), "env")

    def test_without_a_user_key_the_demo_provider_is_used(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "demo-key-value"}):
            self.assertEqual(config.tier_source("small"), "demo (Groq)")
            self.assertEqual(config.get_tier_config("small").model, "openai/gpt-oss-20b")


class KeyEndpointTests(unittest.TestCase):
    def setUp(self):
        runtime_keys.clear_all()
        self.addCleanup(runtime_keys.clear_all)
        # TestClient reports host "testclient"; treat it as local for the
        # allowed paths. test_rejects_a_non_loopback_client patches it back.
        self.enterContext(patch.object(app_module, "LOOPBACK_HOSTS",
                                       {"127.0.0.1", "::1", "localhost", "testclient"}))

    def test_post_stores_and_returns_masked_only(self):
        r = client.post("/config/keys", json={"provider": "openai", "api_key": FAKE_KEY})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"provider": "openai", "connected": True,
                                    "masked": "..." + FAKE_KEY[-4:]})
        self.assertNotIn(FAKE_KEY, r.text)

    def test_delete_disconnects(self):
        client.post("/config/keys", json={"provider": "gemini", "api_key": FAKE_KEY})
        r = client.delete("/config/keys/gemini")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["connected"])
        self.assertFalse(runtime_keys.has_key("gemini"))

    def test_rejects_a_non_loopback_client(self):
        # TestClient reports 'testclient' as the host, which is not loopback.
        with patch.object(app_module, "LOOPBACK_HOSTS", {"203.0.113.9"}):
            for call in (lambda: client.post("/config/keys",
                                             json={"provider": "openai", "api_key": FAKE_KEY}),
                         lambda: client.delete("/config/keys/openai"),
                         lambda: client.get("/config/keys")):
                response = call()
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_invalid_input_is_rejected_without_echoing_it(self):
        r = client.post("/config/keys", json={"provider": "openai", "api_key": "tiny"})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("tiny", r.json()["error"]["message"])

    def test_models_shows_source_and_never_a_key(self):
        client.post("/config/keys", json={"provider": "anthropic", "api_key": FAKE_KEY})
        r = client.get("/models")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(FAKE_KEY, r.text)
        body = r.json()
        self.assertIn("tier_source", body)
        self.assertTrue(body["user_keys"]["anthropic"]["connected"])
        self.assertEqual(body["user_keys"]["anthropic"]["masked"], "..." + FAKE_KEY[-4:])

    def test_chat_contract_is_unchanged_by_this_feature(self):
        with Harness(FakeProvider({"small": ok()})):
            data = post().json()
        for key in ("request_id", "session_id", "answer", "difficulty", "initial_model",
                    "final_model", "quality", "escalated", "attempts", "impact",
                    "baseline", "savings", "summary"):
            self.assertIn(key, data)


class AnthropicAdapterTests(unittest.IsolatedAsyncioTestCase):
    """Native Messages API adapter, against a fake transport. No network."""

    ENV = {"BIG_BASE_URL": "https://api.anthropic.com/v1", "BIG_API_KEY": FAKE_KEY,
           "BIG_MODEL": "claude-opus-5-5"}

    def setUp(self):
        self.requests = []
        self.env = patch.dict(os.environ, self.ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def use_transport(self, handler):
        def recording(request):
            self.requests.append(request)
            return handler(request)
        return patch.object(model_clients, "_make_client",
                            lambda: httpx.AsyncClient(transport=httpx.MockTransport(recording)))

    async def test_parses_content_blocks_and_usage(self):
        body = {"content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": " there"}],
                "usage": {"input_tokens": 11, "output_tokens": 4}}
        with self.use_transport(lambda r: httpx.Response(200, json=body)):
            result = await model_clients.call_model("big", "hi")
        self.assertEqual(result["answer"], "Hello there")
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (11, 4))
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.headers["anthropic-version"], model_clients.ANTHROPIC_VERSION)

    async def test_key_travels_in_a_header_not_the_url(self):
        body = {"content": [{"type": "text", "text": "ok"}], "usage": {}}
        with self.use_transport(lambda r: httpx.Response(200, json=body)):
            await model_clients.call_model("big", "hi")
        request = self.requests[0]
        self.assertEqual(request.headers["x-api-key"], FAKE_KEY)
        self.assertNotIn(FAKE_KEY, str(request.url))

    async def assert_code(self, handler, code, retryable):
        with self.use_transport(handler):
            with self.assertRaises(ProviderError) as ctx:
                await model_clients.call_model("big", "hi")
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.retryable, retryable)
        self.assertNotIn(FAKE_KEY, ctx.exception.message)
        return ctx.exception

    async def test_error_codes_map_to_ours(self):
        await self.assert_code(lambda r: httpx.Response(401, text="no"), "AUTH_FAILED", False)
        await self.assert_code(lambda r: httpx.Response(429, text="slow"), "RATE_LIMITED", True)
        await self.assert_code(lambda r: httpx.Response(529, text="busy"), "PROVIDER_ERROR", True)
        await self.assert_code(lambda r: httpx.Response(400, text="bad"), "PROVIDER_ERROR", False)

    async def test_empty_and_malformed_bodies_are_errors(self):
        await self.assert_code(
            lambda r: httpx.Response(200, json={"content": [], "usage": {}}), "EMPTY_ANSWER", True)
        await self.assert_code(lambda r: httpx.Response(200, text="<html>"), "BAD_RESPONSE", True)

    async def test_timeout_maps_to_our_timeout_code(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        await self.assert_code(handler, "TIMEOUT", True)


class VerifyKeyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        runtime_keys.clear_all()
        self.addCleanup(runtime_keys.clear_all)

    def transport(self, handler):
        return patch.object(model_clients, "_make_client",
                            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_success_and_failure_codes_without_leaking(self):
        runtime_keys.set_key("openai", FAKE_KEY)
        cases = [(200, True, "OK"), (401, False, "AUTH_FAILED"),
                 (429, False, "RATE_LIMITED"), (500, False, "PROVIDER_ERROR")]
        for status, ok_flag, code in cases:
            with self.subTest(status=status):
                with self.transport(lambda r, s=status: httpx.Response(s, text="body")):
                    result = await model_clients.verify_key("openai")
                self.assertEqual((result["ok"], result["code"]), (ok_flag, code))
                self.assertNotIn(FAKE_KEY, json.dumps(result))

    async def test_no_key_stored(self):
        result = await model_clients.verify_key("gemini")
        self.assertEqual(result["code"], "NO_KEY")


class ModelClientTests(unittest.IsolatedAsyncioTestCase):
    """Provider adapter against a fake HTTP transport (TEST FAKE, no network)."""

    ENV = {"SMALL_BASE_URL": "https://provider.test/v1/", "SMALL_API_KEY": "key-123", "SMALL_MODEL": "model-s"}

    def setUp(self):
        self.requests = []
        self.env = patch.dict(os.environ, self.ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def use_transport(self, handler):
        def recording(request):
            self.requests.append(request)
            return handler(request)
        return patch.object(model_clients, "_make_client",
                            lambda: httpx.AsyncClient(transport=httpx.MockTransport(recording)))

    async def test_success_maps_fields_and_sends_the_expected_request(self):
        body = {"choices": [{"message": {"content": "2"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        with self.use_transport(lambda r: httpx.Response(200, json=body)):
            result = await model_clients.call_model("small", "What is 1 + 1?")
        self.assertEqual(result["answer"], "2")
        self.assertEqual(result["model_name"], "model-s")
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (12, 3))
        self.assertIsInstance(result["latency_ms"], int)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://provider.test/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer key-123")
        sent = json.loads(request.read())
        self.assertEqual(sent["model"], "model-s")
        self.assertEqual(sent["messages"], [{"role": "user", "content": "What is 1 + 1?"}])

    async def test_missing_usage_gives_none_tokens(self):
        with self.use_transport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})):
            result = await model_clients.call_model("small", "hi")
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["output_tokens"])

    async def assert_error(self, handler, code, retryable):
        with self.use_transport(handler):
            with self.assertRaises(ProviderError) as ctx:
                await model_clients.call_model("small", "hi")
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.retryable, retryable)
        self.assertNotIn("key-123", ctx.exception.message)
        return ctx.exception

    async def test_rate_limit_is_retryable(self):
        await self.assert_error(lambda r: httpx.Response(429, text="slow down"), "RATE_LIMITED", True)

    async def test_bad_key_is_not_retryable(self):
        await self.assert_error(lambda r: httpx.Response(401, text="no"), "AUTH_FAILED", False)

    async def test_server_error_is_retryable_and_client_error_is_not(self):
        await self.assert_error(lambda r: httpx.Response(503, text="down"), "PROVIDER_ERROR", True)
        await self.assert_error(lambda r: httpx.Response(400, text="bad model"), "PROVIDER_ERROR", False)

    async def test_timeout_is_retryable(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        error = await self.assert_error(handler, "TIMEOUT", True)
        self.assertEqual(error.model_name, "model-s")

    async def test_network_failure_is_retryable(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)
        await self.assert_error(handler, "NETWORK_ERROR", True)

    async def test_empty_or_malformed_answers_are_errors_not_answers(self):
        await self.assert_error(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}),
                                "EMPTY_ANSWER", True)
        await self.assert_error(lambda r: httpx.Response(200, json={"unexpected": True}), "BAD_RESPONSE", True)
        await self.assert_error(lambda r: httpx.Response(200, text="<html>"), "BAD_RESPONSE", True)

    async def test_optional_parameters_change_the_request_only_when_passed(self):
        body = {"choices": [{"message": {"content": "easy"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 1}}
        with self.use_transport(lambda r: httpx.Response(200, json=body)):
            await model_clients.call_model("small", "hi", instructions="be brief", max_tokens=5)
        sent = json.loads(self.requests[0].read())
        self.assertEqual(sent["messages"], [{"role": "system", "content": "be brief"},
                                            {"role": "user", "content": "hi"}])
        self.assertEqual(sent["max_tokens"], 5)

    async def test_classify_uses_the_classifier_tier_and_returns_raw_text(self):
        body = {"choices": [{"message": {"content": " Hard "}}], "usage": {"prompt_tokens": 7, "completion_tokens": 1}}
        env = {"CLASSIFIER_TIER": "small", "CLASSIFIER_TIMEOUT_SECONDS": "3"}
        with patch.dict(os.environ, env), self.use_transport(lambda r: httpx.Response(200, json=body)):
            result = await model_clients.classify("hi", instructions="classify this")
        self.assertEqual(result["answer"], " Hard ")
        self.assertEqual(result["model_name"], "model-s")
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (7, 1))
        sent = json.loads(self.requests[0].read())
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "classify this"})

    async def test_classifier_timeout_raises_and_never_retries(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        with patch.dict(os.environ, {"CLASSIFIER_TIMEOUT_SECONDS": "1"}), self.use_transport(handler):
            with self.assertRaises(ProviderError) as ctx:
                await model_clients.classify("hi", instructions="x")
        self.assertEqual(ctx.exception.code, "TIMEOUT")
        self.assertEqual(len(self.requests), 1)

    async def test_unconfigured_tier_fails_before_any_network_call(self):
        unset = {"MEDIUM_BASE_URL": "", "MEDIUM_API_KEY": "", "MEDIUM_MODEL": ""}
        # Empty catalog: this test is about the environment-variable layer alone.
        with patch.object(config, "load_catalog", lambda *a, **k: []), \
                patch.dict(os.environ, unset), \
                self.use_transport(lambda r: httpx.Response(200, json={})):
            with self.assertRaises(ProviderError) as ctx:
                await model_clients.call_model("medium", "hi")
        self.assertEqual(ctx.exception.code, "CONFIG_MISSING")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(self.requests, [])


class ConfigTests(unittest.TestCase):
    def test_env_file_loader_skips_comments_and_never_overrides_real_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text('# comment\nGP_TEST_A=from-file\nGP_TEST_B="quoted"\n\nGP_TEST_C=from-file\n',
                            encoding="utf-8")
            with patch.dict(os.environ, {"GP_TEST_C": "from-environment"}):
                for key in ("GP_TEST_A", "GP_TEST_B"):
                    os.environ.pop(key, None)
                config._load_env_file(path)
                self.assertEqual(os.environ["GP_TEST_A"], "from-file")
                self.assertEqual(os.environ["GP_TEST_B"], "quoted")
                self.assertEqual(os.environ["GP_TEST_C"], "from-environment")
                for key in ("GP_TEST_A", "GP_TEST_B"):
                    os.environ.pop(key, None)

    def test_missing_env_file_is_fine(self):
        config._load_env_file(Path("does-not-exist.env"))

    def test_tier_configured_only_when_all_three_settings_exist(self):
        # Empty catalog: this test is about the environment-variable layer alone.
        self.enterContext(patch.object(config, "load_catalog", lambda *a, **k: []))
        with patch.dict(os.environ, {"SMALL_BASE_URL": "https://a.test", "SMALL_API_KEY": "k", "SMALL_MODEL": ""}):
            self.assertFalse(config.get_tier_config("small").configured)
        with patch.dict(os.environ, {"SMALL_BASE_URL": "https://a.test/", "SMALL_API_KEY": "k", "SMALL_MODEL": "m"}):
            cfg = config.get_tier_config("small")
            self.assertTrue(cfg.configured)
            self.assertEqual(cfg.base_url, "https://a.test")


# ---------- TEST FIXTURES for the model catalog (not the real file) ----------
def fixture_catalog():
    """TEST FIXTURE. Mirrors backend/model_catalog.json's shape, not its contents."""
    return [
        {"id": "fixture/small-a", "provider": "fixture-co", "label": "Fixture Small A", "tier": "small",
         "key_env": "FIXTURE_UNSET_KEY", "base_url": "https://a.fixture/v1", "enabled": True, "note": "n"},
        {"id": "fixture/small-b", "provider": "fixture-co", "label": "Fixture Small B", "tier": "small",
         "key_env": "FIXTURE_SET_KEY", "base_url": "https://b.fixture/v1/", "enabled": True, "note": "n"},
        {"id": "fixture/small-c", "provider": "fixture-co", "label": "Fixture Small C", "tier": "small",
         "key_env": "FIXTURE_SET_KEY", "base_url": "https://c.fixture/v1", "enabled": True, "note": "n"},
        {"id": "fixture/medium-off", "provider": "fixture-co", "label": "Fixture Medium", "tier": "medium",
         "key_env": "FIXTURE_SET_KEY", "base_url": "https://m.fixture/v1", "enabled": False, "note": "n"},
    ]


class ModelCatalogTests(unittest.TestCase):
    """The catalog layer. No network, no real catalog file, no real keys."""

    def setUp(self):
        self.enterContext(patch.object(config, "load_catalog", lambda *a, **k: fixture_catalog()))
        self.enterContext(patch.dict(os.environ, {"FIXTURE_SET_KEY": "fixture-key-value"}))
        os.environ.pop("FIXTURE_UNSET_KEY", None)
        # Clear the per-tier overrides so the catalog layer is reachable.
        self.enterContext(patch.dict(os.environ, {
            f"{t}_{s}": "" for t in ("SMALL", "MEDIUM", "BIG")
            for s in ("BASE_URL", "API_KEY", "MODEL")}))

    def test_first_enabled_entry_with_a_key_present_wins(self):
        entry = config.catalog_entry_for_tier("small")
        # small-a is listed first but its key_env is unset, so small-b is chosen.
        self.assertEqual(entry["id"], "fixture/small-b")

    def test_disabled_entries_are_never_chosen(self):
        self.assertIsNone(config.catalog_entry_for_tier("medium"))

    def test_catalog_supplies_the_tier_config_and_strips_trailing_slash(self):
        cfg = config.get_tier_config("small")
        self.assertEqual(cfg.model, "fixture/small-b")
        self.assertEqual(cfg.base_url, "https://b.fixture/v1")
        self.assertTrue(cfg.configured)

    def test_explicit_env_vars_win_over_the_catalog(self):
        override = {"SMALL_BASE_URL": "https://override.test/v1",
                    "SMALL_API_KEY": "override-key", "SMALL_MODEL": "override-model"}
        with patch.dict(os.environ, override):
            cfg = config.get_tier_config("small")
        self.assertEqual(cfg.model, "override-model")
        self.assertEqual(cfg.base_url, "https://override.test/v1")

    def test_a_partial_env_override_does_not_beat_the_catalog(self):
        with patch.dict(os.environ, {"SMALL_MODEL": "half-configured"}):
            self.assertEqual(config.get_tier_config("small").model, "fixture/small-b")

    def test_tier_with_no_usable_entry_stays_unconfigured(self):
        self.assertFalse(config.get_tier_config("big").configured)


class CatalogLoadingTests(unittest.TestCase):
    """load_catalog never raises and never trusts the file blindly."""

    def write(self, text):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "model_catalog.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_returns_empty(self):
        self.assertEqual(config.load_catalog(Path("no-such-catalog.json")), [])

    def test_malformed_json_returns_empty_instead_of_raising(self):
        self.assertEqual(config.load_catalog(self.write("{not json")), [])

    def test_entries_with_a_bad_tier_or_missing_fields_are_dropped(self):
        path = self.write(json.dumps({"models": [
            {"id": "a", "provider": "p", "label": "L", "tier": "enormous",
             "key_env": "K", "base_url": "u", "enabled": True},
            {"id": "b", "provider": "p", "label": "L", "tier": "small", "enabled": True},
            "not-a-dict",
            {"id": "c", "provider": "p", "label": "L", "tier": "small",
             "key_env": "K", "base_url": "u", "enabled": True},
        ]}))
        kept = config.load_catalog(path)
        self.assertEqual([e["id"] for e in kept], ["c"])

    def test_real_catalog_file_parses_and_covers_every_tier(self):
        entries = config.load_catalog()
        self.assertTrue(entries, "backend/model_catalog.json failed to load")
        for tier in TIERS:
            self.assertTrue([e for e in entries if e["tier"] == tier], tier)
        for entry in entries:
            self.assertNotIn("api_key", entry)
            self.assertTrue(entry["key_env"].isupper())


class ModelsEndpointTests(unittest.TestCase):
    def test_reports_live_and_not_connected_without_exposing_keys(self):
        with patch.object(config, "load_catalog", lambda *a, **k: fixture_catalog()), \
                patch.dict(os.environ, {"FIXTURE_SET_KEY": "super-secret-key"}):
            os.environ.pop("FIXTURE_UNSET_KEY", None)
            response = client.get("/models")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("super-secret-key", response.text)
        by_id = {row["id"]: row for row in response.json()["models"]}
        for key in ("label", "provider", "tier", "status"):
            self.assertIn(key, by_id["fixture/small-b"])
        self.assertEqual(by_id["fixture/small-b"]["status"], "live")
        self.assertEqual(by_id["fixture/small-a"]["status"], "not_connected")
        self.assertEqual(by_id["fixture/medium-off"]["status"], "not_connected")

    def test_active_names_the_entry_each_tier_would_use(self):
        # Clear the per-tier overrides so the catalog layer is the one under test.
        cleared = {f"{t}_{s}": "" for t in ("SMALL", "MEDIUM", "BIG")
                   for s in ("BASE_URL", "API_KEY", "MODEL")}
        with patch.object(config, "load_catalog", lambda *a, **k: fixture_catalog()), \
                patch.dict(os.environ, {"FIXTURE_SET_KEY": "k", **cleared}):
            os.environ.pop("FIXTURE_UNSET_KEY", None)
            data = client.get("/models").json()
        self.assertEqual(data["active"]["small"], {"id": "fixture/small-b", "source": "demo (Groq)"})
        self.assertEqual(data["active"]["medium"]["source"], "none")
        self.assertIsNone(data["active"]["big"]["id"])

    def test_active_reports_env_source_when_a_tier_is_overridden(self):
        override = {"SMALL_BASE_URL": "https://override.test/v1",
                    "SMALL_API_KEY": "override-key", "SMALL_MODEL": "override-model"}
        with patch.object(config, "load_catalog", lambda *a, **k: fixture_catalog()),                 patch.dict(os.environ, {"FIXTURE_SET_KEY": "k", **override}):
            data = client.get("/models").json()
        # The catalog still lists small-b as live, but the tier really uses the override.
        self.assertEqual(data["active"]["small"], {"id": "override-model", "source": "env"})

    def test_real_catalog_is_served_without_any_key_shaped_string(self):
        response = client.get("/models")
        self.assertEqual(response.status_code, 200)
        self.assertNotRegex(response.text, r"gsk_[A-Za-z0-9]{10,}")
        self.assertNotRegex(response.text, r"sk-[A-Za-z0-9]{20,}")
        self.assertIn("tier_note", response.json())


if __name__ == "__main__":
    unittest.main()
