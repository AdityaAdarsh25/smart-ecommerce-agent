"""The one place this project talks to a language model.

Deliberately tiny. It is a function, not a framework: a single JSON-mode
completion against the official OpenAI SDK, configured entirely from the
environment. Swapping providers later means rewriting `complete_json`,
not unpicking an abstraction layer.

Nothing financial passes through here. The model is asked to read language
and rank products; it is never asked whether money may be spent. That
question belongs to `backend/policies/policy_engine.py`.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from dotenv import load_dotenv

load_dotenv()

# Environment variable names. Values live in `.env`, never in source.
ENV_API_KEY = "OPENAI_API_KEY"
ENV_MODEL = "OPENAI_MODEL"
ENV_BASE_URL = "OPENAI_BASE_URL"

DEFAULT_TIMEOUT_SECONDS = 30.0

# A reply that is not usable JSON is retried a bounded number of times
# before the request is refused. Asking the same question again is safe:
# this call reads language, touches no money and changes no state. A
# malformed reply is still never used, however many times it arrives.
MAX_JSON_ATTEMPTS = 3

# An upper bound on a provider's "try again in N seconds" hint. The value
# is provider-supplied text, so an implausible one is reported as no hint
# at all rather than passed to a caller as guidance.
MAX_RETRY_AFTER_SECONDS = 3600.0

# The callable shape the agent depends on. Anything matching it can stand
# in -- which is how the tests supply deterministic responses without a
# provider, a recorded cassette or a network socket.
LLMCallable = Callable[..., dict[str, Any]]


class LLMConfigurationError(RuntimeError):
    """The model is not configured, so no AI ran.

    Raised loudly and early rather than degrading to a silent heuristic
    that would let a demo claim an AI parsed a request when none did.
    """


class LLMResponseError(RuntimeError):
    """The model replied with something that is not usable JSON.

    A malformed reply is a failure, never a licence to guess.
    """


class LLMRateLimitError(RuntimeError):
    """The provider refused the call because a rate limit was reached.

    Deliberately distinct from `LLMResponseError`: nothing was wrong with
    the request and no reply was produced to validate, so the same request
    may succeed shortly. It is never retried here -- retrying into a
    limiter is how a short pause becomes a long outage.

    `retry_after_seconds` is the provider's own hint when it sent one, and
    None otherwise. Nothing depends on it being present.
    """

    def __init__(
        self, message: str, *, retry_after_seconds: Optional[float] = None
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    model: str
    base_url: Optional[str] = None


def load_llm_config() -> LLMConfig:
    """Read the provider configuration from the environment.

    Missing configuration is a controlled, explanatory failure. It names
    the variable that is absent, because the alternative -- a 500 from
    somewhere inside an SDK -- tells an operator nothing.
    """
    api_key = (os.getenv(ENV_API_KEY) or "").strip()
    model = (os.getenv(ENV_MODEL) or "").strip()

    missing = [
        name
        for name, value in ((ENV_API_KEY, api_key), (ENV_MODEL, model))
        if not value
    ]
    if missing:
        raise LLMConfigurationError(
            "The AI buyer agent is not configured: "
            f"{', '.join(missing)} is not set. "
            "Set it in your environment (see .env.example). "
            "No AI request was made and nothing was quoted."
        )

    base_url = (os.getenv(ENV_BASE_URL) or "").strip() or None
    return LLMConfig(api_key=api_key, model=model, base_url=base_url)


def complete_json(
    *,
    system: str,
    user: str,
    config: Optional[LLMConfig] = None,
) -> dict[str, Any]:
    """One JSON-mode chat completion. Returns the parsed object.

    The model is pinned to JSON output because every caller in this
    project wants a structure it can validate, not prose it must parse.
    Validation of that structure still happens at the call site -- being
    well-formed JSON is not the same as being trustworthy.
    """
    config = config or load_llm_config()

    try:
        from openai import BadRequestError, OpenAI, RateLimitError
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise LLMConfigurationError(
            "The `openai` package is not installed, so the AI buyer agent "
            "cannot run. Install the project requirements."
        ) from exc

    client = OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )

    # The SDK retries a 429 twice by default, before this code sees it.
    # On a metered free tier that spends quota we do not have on a request
    # already known to be throttled, and it delays the refusal by the
    # provider's own backoff. Turned off for this request only, so the
    # limit surfaces on the first answer. This is unrelated to the bounded
    # JSON retry below, which is ours and stays.
    provider = client.with_options(max_retries=0)

    last_error: Optional[Exception] = None
    for _attempt in range(MAX_JSON_ATTEMPTS):
        try:
            completion = provider.chat.completions.create(
                model=config.model,
                response_format={"type": "json_object"},
                # Deterministic-leaning: this is an extraction and ranking
                # task, not a creative one.
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except RateLimitError as exc:
            # The SDK's own 429 type, so the check is the same for any
            # OpenAI-compatible endpoint. Surfaced immediately and never
            # retried by this loop: the bounded retry below exists for
            # unusable JSON, which is a different problem with a different
            # cure. Nothing was quoted on the way here.
            raise LLMRateLimitError(
                "The AI provider is rate limited.",
                retry_after_seconds=_retry_after_seconds(exc),
            ) from exc
        except BadRequestError as exc:
            # Some OpenAI-compatible providers enforce JSON mode on their
            # side and answer with a 400 when their own model's reply fails
            # that check, instead of returning the text. That is a
            # malformed reply, not a misconfiguration, so it is treated as
            # one. Any other 400 is a real request problem and propagates.
            if not _is_json_generation_failure(exc):
                raise
            last_error = exc
            continue

        content = completion.choices[0].message.content or ""
        try:
            return parse_json_object(content)
        except LLMResponseError as exc:
            last_error = exc

    # Every attempt produced something unusable. Nothing is guessed and
    # nothing is returned -- the caller refuses the request.
    raise LLMResponseError(
        "The language model did not return valid JSON in "
        f"{MAX_JSON_ATTEMPTS} attempts."
    ) from last_error


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """The provider's `Retry-After` hint, if it sent a usable one.

    Read defensively at every step. The header is optional, its value is
    provider-supplied text, and the response object differs between SDK
    versions -- so anything unexpected becomes None rather than an error
    on the way to reporting an error.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None

    try:
        raw = headers.get("retry-after")
    except Exception:  # pragma: no cover - defensive, headers is mapping-like
        return None
    if raw is None:
        return None

    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None

    if seconds < 0 or seconds > MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds


def _is_json_generation_failure(exc: Exception) -> bool:
    """Did the provider reject its own model's reply as invalid JSON?

    Matched on the provider's wording rather than a vendor-specific error
    class, so the check holds for any OpenAI-compatible endpoint that
    validates JSON mode server-side.
    """
    code = str(getattr(exc, "code", "") or "")
    return "json" in code.lower() or "json" in str(exc).lower()


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse a model reply that is required to be a JSON object."""
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMResponseError(
            "The language model did not return valid JSON."
        ) from exc

    if not isinstance(parsed, dict):
        raise LLMResponseError(
            "The language model returned JSON that is not an object."
        )
    return parsed


def get_llm() -> LLMCallable:
    """FastAPI dependency for the model call.

    Exists so the route depends on the *seam* rather than the provider.
    Overriding it is how the suite guarantees no test reaches a real model.
    """
    return complete_json
