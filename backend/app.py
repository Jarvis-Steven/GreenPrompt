"""Owner: backend member. HTTP layer and the orchestration loop.

The backend controls the loop; Jarvis's functions make the decisions and the
metrics member's functions do the accounting (docs/team-workflow.md).

Per request: classify -> choose a starting tier -> call the model -> check the
answer -> ask router.next_model what to do -> repeat (never the same tier twice,
never below the last tier). Every call becomes an attempt record.
"""
import logging
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from backend import dev_stubs, metrics, model_clients, router, storage, validator
from backend.config import (
    ALLOWED_ORIGINS, catalog_status, dev_stubs_enabled, get_tier_config, provider_status,
    tier_source,
)
from backend.model_clients import ProviderError
from backend.schemas import (
    Attempt, ChatRequest, ChatResponse, Classifier, ErrorBody, ErrorResponse, Impact, Metrics,
    Quality, Summary,
)

log = logging.getLogger("greenprompt")

TIER_ORDER = ["small", "medium", "big"]
DIFFICULTIES = ("easy", "medium", "hard")
QUALITY_STATUSES = ("passed", "failed", "unchecked")
# Used when the classifier cannot produce a difficulty. Never a claim about the prompt.
FALLBACK_DIFFICULTY = "medium"
STUB_HEADER = "X-GreenPrompt-Dev-Stubs"

app = FastAPI(title="GreenPrompt", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    expose_headers=[STUB_HEADER],
)


class ComponentNotReady(Exception):
    """A teammate's function still raises NotImplementedError."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


class ChatFailure(Exception):
    """A request that must end with an error envelope."""

    def __init__(self, status: int, code: str, message: str, retryable: bool):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


def _error(request_id: str, status: int, code: str, message: str, retryable: bool) -> JSONResponse:
    body = ErrorResponse(request_id=request_id, error=ErrorBody(code=code, message=message, retryable=retryable))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def _component(name, real, stub, used, *args):
    """Call a team function. If it is unfinished, fail honestly, or (only when
    GREENPROMPT_DEV_STUBS=1) use the labelled development stub and record that."""
    try:
        return real(*args)
    except NotImplementedError:
        if not dev_stubs_enabled():
            raise ComponentNotReady(name)
        if name not in used:
            used.append(name)
        return stub(*args)


def _team_fn(module, name: str):
    """A teammate's function, or a stand-in that reports it is not ready.

    classify_with_rules and classify_from_model_output are not in router.py yet,
    so a missing attribute has to behave exactly like NotImplementedError.
    """
    fn = getattr(module, name, None)
    if callable(fn):
        return fn

    def _not_ready(*args, **kwargs):
        raise NotImplementedError(name)
    return _not_ready


def _classifier_instructions(used: list) -> str:
    """Jarvis's wording when router.CLASSIFIER_INSTRUCTIONS exists, otherwise the
    clearly labelled development placeholder."""
    text = getattr(router, "CLASSIFIER_INSTRUCTIONS", None)
    if isinstance(text, str) and text.strip():
        return text
    if not dev_stubs_enabled():
        raise ComponentNotReady("router.CLASSIFIER_INSTRUCTIONS")
    if "router.CLASSIFIER_INSTRUCTIONS" not in used:
        used.append("router.CLASSIFIER_INSTRUCTIONS")
    return dev_stubs.CLASSIFIER_INSTRUCTIONS


def _classifier_record(*, used_call: bool, status: str, method: str, difficulty: str, reason: str,
                       model_name=None, latency_ms=None, input_tokens=None, output_tokens=None) -> dict:
    return {
        "used": used_call, "status": status, "method": method, "model_name": model_name,
        "latency_ms": latency_ms, "input_tokens": input_tokens, "output_tokens": output_tokens,
        "difficulty": difficulty, "reason": reason,
    }


def _read_rules(result) -> tuple:
    """(difficulty or None, reason). Anything malformed counts as "cannot decide"."""
    if not isinstance(result, dict):
        log.warning("router.classify_with_rules returned %r, not a dict; treating as undecided", type(result))
        return None, "The rules pass returned an unusable result; treated as undecided."
    difficulty = result.get("difficulty")
    reason = result.get("reason", "") if isinstance(result.get("reason"), str) else ""
    if difficulty is None:
        return None, reason
    if difficulty not in DIFFICULTIES:
        log.warning("router.classify_with_rules returned invalid difficulty %r; treating as undecided", difficulty)
        return None, "The rules pass returned an invalid difficulty; treated as undecided."
    return difficulty, reason


async def _classify(req: ChatRequest, used: list) -> tuple:
    """Hybrid classification. Returns (difficulty, classifier record).

    Rules first. Only when the rules cannot decide, and only in smart mode, does
    one model call happen. Any failure falls back to a labelled default
    difficulty and keeps the failed call's usage details.
    """
    rules = _component("router.classify_with_rules", _team_fn(router, "classify_with_rules"),
                       dev_stubs.classify_with_rules, used, req.prompt)
    difficulty, reason = _read_rules(rules)

    if difficulty is not None:
        return difficulty, _classifier_record(used_call=False, status="skipped", method="rules",
                                              difficulty=difficulty, reason=reason)

    if req.mode == "pick":
        # Pick mode never calls the model classifier. The tier is the user's
        # choice either way; difficulty is reported for display only.
        return FALLBACK_DIFFICULTY, _classifier_record(
            used_call=False, status="skipped", method="fallback", difficulty=FALLBACK_DIFFICULTY,
            reason="No rule matched. Pick mode never calls the model classifier, so the "
                   "default difficulty %r was reported." % FALLBACK_DIFFICULTY)

    instructions = _classifier_instructions(used)
    try:
        result = await model_clients.classify(req.prompt, instructions=instructions)
    except ProviderError as exc:
        log.warning("classifier call failed: %s: %s", exc.code, exc.message)
        return FALLBACK_DIFFICULTY, _classifier_record(
            used_call=True, status="timeout" if exc.code == "TIMEOUT" else "error", method="fallback",
            difficulty=FALLBACK_DIFFICULTY, reason="%s: %s" % (exc.code, exc.message),
            model_name=exc.model_name or None, latency_ms=exc.latency_ms or None)
    except Exception:
        log.exception("unexpected error in the classifier client")
        return FALLBACK_DIFFICULTY, _classifier_record(
            used_call=True, status="error", method="fallback", difficulty=FALLBACK_DIFFICULTY,
            reason="INTERNAL: unexpected error in the classifier client.")

    # The call succeeded, so its usage is real and is kept whatever the parser says.
    usage = {"model_name": result.get("model_name"), "latency_ms": result.get("latency_ms"),
             "input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens")}
    try:
        parsed = _component("router.classify_from_model_output",
                            _team_fn(router, "classify_from_model_output"),
                            dev_stubs.classify_from_model_output, used, req.prompt, result["answer"])
        if not isinstance(parsed, dict) or parsed.get("difficulty") not in DIFFICULTIES:
            raise ValueError("parser returned %r" % (parsed,))
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("classifier output could not be parsed: %s", exc)
        return FALLBACK_DIFFICULTY, _classifier_record(
            used_call=True, status="invalid_output", method="fallback", difficulty=FALLBACK_DIFFICULTY,
            reason="The classifier output could not be parsed (%s); used the default difficulty %r."
                   % (type(exc).__name__, FALLBACK_DIFFICULTY), **usage)

    return parsed["difficulty"], _classifier_record(
        used_call=True, status="success", method="model", difficulty=parsed["difficulty"],
        reason=parsed.get("reason", "") if isinstance(parsed.get("reason"), str) else "", **usage)


async def _run_attempt(tier: str, prompt: str, used: list):
    """One provider call plus its quality check. Returns (attempt, answer, retryable)."""
    started = time.perf_counter()
    try:
        result = await model_clients.call_model(tier, prompt)
    except ProviderError as exc:
        reason = f"{exc.code}: {exc.message}"
        log.warning("tier=%s call failed: %s", tier, reason)
        attempt = {
            "tier": tier, "model_name": exc.model_name or "unknown", "status": "error",
            "latency_ms": exc.latency_ms or int((time.perf_counter() - started) * 1000),
            "input_tokens": None, "output_tokens": None,
            "quality_status": "unchecked", "quality_reason": reason,
        }
        return attempt, None, exc.retryable
    except Exception:                      # a bug in the client must not look like an answer
        log.exception("tier=%s unexpected client error", tier)
        attempt = {
            "tier": tier, "model_name": "unknown", "status": "error",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "input_tokens": None, "output_tokens": None,
            "quality_status": "unchecked", "quality_reason": "INTERNAL: unexpected error in the model client.",
        }
        return attempt, None, False

    check = _component("validator.check_answer", validator.check_answer, dev_stubs.check_answer,
                       used, prompt, result["answer"])
    status = check.get("status") if isinstance(check, dict) else None
    reason = check.get("reason", "") if isinstance(check, dict) else ""
    if status not in QUALITY_STATUSES:
        log.warning("validator returned invalid status %r; treating as unchecked", status)
        status, reason = "unchecked", "The validator returned an invalid result; treated as unchecked."
    attempt = {
        "tier": tier, "model_name": result["model_name"], "status": "success",
        "latency_ms": result["latency_ms"],
        "input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens"),
        "quality_status": status, "quality_reason": reason,
    }
    return attempt, result["answer"], False


async def run_chat(req: ChatRequest, request_id: str):
    used: list = []

    difficulty, classifier = await _classify(req, used)
    tier = _component("router.choose_model", router.choose_model, dev_stubs.choose_model,
                      used, difficulty, req.mode, req.selected_model)
    if tier not in TIER_ORDER:
        raise ChatFailure(500, "INTERNAL_ERROR", "router.choose_model returned an invalid tier.", False)
    initial = tier

    attempts: list = []
    last_good = None                       # (tier, answer, {"status", "reason"}) of the last returned answer
    any_retryable = False
    tried = set()
    while True:
        tried.add(tier)
        attempt, answer, retryable = await _run_attempt(tier, req.prompt, used)
        attempts.append(attempt)
        any_retryable = any_retryable or retryable
        if answer is not None:
            last_good = (tier, answer, {"status": attempt["quality_status"], "reason": attempt["quality_reason"]})
        nxt = _component("router.next_model", router.next_model, dev_stubs.next_model,
                         used, tier, attempt["quality_status"], attempt["status"])
        if nxt is None:
            break
        if nxt not in TIER_ORDER or nxt in tried or TIER_ORDER.index(nxt) <= TIER_ORDER.index(tier):
            log.warning("router.next_model returned %r after %s; stopping (no repeats, no downgrades)", nxt, tier)
            break
        tier = nxt

    if last_good is None:
        detail = "; ".join(f"{a['tier']}: {a['quality_reason']}" for a in attempts)
        log.warning("request %s failed on every attempt: %s", request_id, detail)
        raise ChatFailure(503, "MODEL_UNAVAILABLE",
                          f"No model could complete this request. Attempts: {detail}", any_retryable)

    final_tier, answer_text, quality = last_good
    escalated = any(a["tier"] != initial for a in attempts)

    try:
        computed = _component("metrics.calculate_metrics", metrics.calculate_metrics, dev_stubs.calculate_metrics,
                              used, [dict(a) for a in attempts])
        impact = Impact(**computed["impact"])
        baseline = Metrics(**computed["baseline"])
        savings = Metrics(**computed["savings"])
        record = {
            "request_id": request_id, "session_id": req.session_id, "prompt": req.prompt, "mode": req.mode,
            "difficulty": difficulty, "initial_model": initial, "final_model": final_tier,
            "answer": answer_text, "quality": quality, "escalated": escalated, "attempts": attempts,
            "impact": impact.model_dump(), "baseline": baseline.model_dump(), "savings": savings.model_dump(),
            # Passed through untouched for the metrics member; not part of attempts.
            "classifier": dict(classifier),
        }
        summary = Summary(**_component("storage.record_result", storage.record_result, dev_stubs.record_result,
                                       used, record))
        response = ChatResponse(
            request_id=request_id, session_id=req.session_id, answer=answer_text, difficulty=difficulty,
            classifier=Classifier(**classifier),
            initial_model=initial, final_model=final_tier, quality=Quality(**quality), escalated=escalated,
            attempts=[Attempt(**a) for a in attempts], impact=impact, baseline=baseline, savings=savings,
            summary=summary,
        )
    except (ValidationError, KeyError, TypeError) as exc:
        log.exception("metrics/storage output does not match docs/api-contract.md")
        raise ChatFailure(500, "INTERNAL_ERROR",
                          f"metrics or storage returned data that does not match the contract ({type(exc).__name__}).",
                          False)
    return response, used


@app.get("/health")
def health():
    return {"status": "ok", "service": "GreenPrompt", "stage": "scaffold",
            "providers": provider_status(), "dev_stubs": dev_stubs_enabled()}


@app.get("/models")
def models():
    """The model catalog: which real models map to our small/medium/big labels.

    "tier" is GreenPrompt's own size label, not an official provider ranking.
    status is "live" when the entry is enabled and its key_env variable is set.
    Never returns an API key.
    """
    rows = catalog_status()
    return {
        "models": rows,
        "active": {tier: _active_for(tier, rows) for tier in TIER_ORDER},
        "tier_note": "tier is GreenPrompt's own size label, not an official provider ranking.",
    }


def _active_for(tier: str, rows: list):
    """What this tier ACTUALLY resolves to right now, and where it came from.

    Reports the real resolved config rather than the catalog's preference, so a
    per-tier environment override is never misreported as a catalog choice.
    """
    cfg = get_tier_config(tier)
    if not cfg.configured:
        return {"id": None, "source": "none"}
    return {"id": cfg.model, "source": tier_source(tier)}


@app.post("/chat")
async def chat(req: ChatRequest):
    request_id = str(uuid4())
    try:
        response, used = await run_chat(req, request_id)
    except ComponentNotReady as exc:
        return _error(request_id, 501, "NOT_IMPLEMENTED",
                      f"{exc.name} is not implemented yet. A teammate's component is still pending "
                      "(for local development only, set GREENPROMPT_DEV_STUBS=1).", False)
    except ChatFailure as exc:
        return _error(request_id, exc.status, exc.code, exc.message, exc.retryable)
    headers = {STUB_HEADER: ",".join(used)} if used else {}
    return JSONResponse(content=response.model_dump(mode="json"), headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first.get("msg", "Invalid request.")).removeprefix("Value error, ")
    return _error(str(uuid4()), 422, "VALIDATION_ERROR", f"{where}: {message}" if where else message, False)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    log.exception("unhandled error")
    return _error(str(uuid4()), 500, "INTERNAL_ERROR", "Unexpected server error.", True)
