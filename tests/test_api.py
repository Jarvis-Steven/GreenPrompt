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

from backend import config, metrics, model_clients, router, storage, validator
from backend.app import app
from backend.model_clients import ProviderError

client = TestClient(app, raise_server_exceptions=False)

GOOD_BODY = {"session_id": "s-1", "prompt": "What is 1 + 1?", "mode": "smart", "selected_model": None}
TIERS = ["small", "medium", "big"]


# ---------- TEST FAKES (not real components) ----------
def fake_classify(prompt):
    return "easy"


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

    def __init__(self, provider, check=None, next_fn=fake_next, metrics_fn=fake_metrics):
        self.provider = provider
        self.check = check or (lambda prompt, answer: {"status": "passed", "reason": "test: passed"})
        self.patches = [
            patch.object(router, "classify_prompt", fake_classify),
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
            patch.object(router, "classify_prompt", self.pending),
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
        for name in ("router.classify_prompt", "validator.check_answer", "metrics.calculate_metrics",
                     "storage.record_result"):
            self.assertIn(name, header)
        data = response.json()
        self.assertEqual(data["quality"]["status"], "unchecked")
        self.assertIn("DEV STUB", data["quality"]["reason"])


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

    async def test_unconfigured_tier_fails_before_any_network_call(self):
        unset = {"MEDIUM_BASE_URL": "", "MEDIUM_API_KEY": "", "MEDIUM_MODEL": ""}
        with patch.dict(os.environ, unset), self.use_transport(lambda r: httpx.Response(200, json={})):
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
        with patch.dict(os.environ, {"SMALL_BASE_URL": "https://a.test", "SMALL_API_KEY": "k", "SMALL_MODEL": ""}):
            self.assertFalse(config.get_tier_config("small").configured)
        with patch.dict(os.environ, {"SMALL_BASE_URL": "https://a.test/", "SMALL_API_KEY": "k", "SMALL_MODEL": "m"}):
            cfg = config.get_tier_config("small")
            self.assertTrue(cfg.configured)
            self.assertEqual(cfg.base_url, "https://a.test")


if __name__ == "__main__":
    unittest.main()
