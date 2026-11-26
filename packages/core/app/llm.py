"""A minimal OpenAI-compatible chat client, used for the tutor features.

Only the server talks to this; the browser never sees a key. Three things about
the endpoint this targets are worth writing down, because each one cost a
debugging round:

*   It sits behind Cloudflare, which rejects the default ``Python-urllib``
    User-Agent with ``403 error code 1010``. A browser-like UA is required.
*   The model is a reasoning model. If the token budget runs out mid-thought it
    returns *empty content* with ``finish_reason="length"`` -- which is
    indistinguishable from a refusal unless you look at the reasoning-token
    count. So an empty answer with reasoning tokens spent is retried with a
    larger budget rather than returned as "".
*   Calls are slow (tens of seconds). Everything the tutor produces is cached
    against a content hash, so the second learner to open an article pays
    nothing.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .config import is_local_base_url

log = logging.getLogger("diglot.llm")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_UNCLOSED = re.compile(r"<think>.*$", re.S | re.I)


def _strip_thinking(text: str) -> str:
    """Remove inline ``<think>...</think>`` traces emitted by local reasoning models."""
    if not text or "<think" not in text.lower():
        return text
    cleaned = _THINK_BLOCK.sub("", text)
    if "<think" in cleaned.lower():
        cleaned = _THINK_UNCLOSED.sub("", cleaned)
    return cleaned.strip()

# Ceiling for automatic budget escalation. Reasoning models can spend 8k-16k
# hidden thinking tokens on a long weave; providers with lower 8k caps still
# auto-step down cleanly via the HTTP 400 fallback handler.
_MAX_BUDGET = 24_000

_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524, 529}
_MAX_TOKENS_CAP_RE = re.compile(r"(?:max_tokens|maximum|limit|<=|at most)[^0-9]{0,24}(\d{3,6})", re.I)

# Seconds to wait before each retry. Roughly exponential, and long enough to
# outlast a transient DNS or connection failure rather than walking straight
# into it again.
_BACKOFF_SECONDS = (1.0, 5.0, 12.0, 20.0)


def _content_text(message: dict[str, Any]) -> str:
    """Pull the assistant text out of a message.

    ``content`` is a string on most providers, but the OpenAI schema also
    allows a list of typed parts (``[{"type": "text", "text": ...}]``) and some
    gateways return that. Assuming a string here turns a perfectly good answer
    into an AttributeError that looks like a network failure.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return ""


class LLMError(RuntimeError):
    """The model could not be reached, or returned something unusable."""

    def __init__(self, message: str, *, status_code: int | None = None, fatal: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.fatal = fatal


def _read_completion_response(
    response: Any, *, cancel_check: Callable[[], None] | None = None
) -> dict[str, Any]:
    """Read either a standard JSON response or an OpenAI-compatible SSE stream."""
    headers = getattr(response, "headers", None)
    content_type = str(headers.get("Content-Type", "") if headers else "").lower()
    if "text/event-stream" not in content_type:
        raw = response.read()
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", "replace")
        else:
            text = str(raw)
        stripped = text.lstrip()
        if not stripped.startswith("data:"):
            return json.loads(stripped)
        lines = text.splitlines()
    else:
        lines = None

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = ""
    model_name = ""
    usage: dict[str, Any] = {}

    def consume_line(raw_line: Any) -> bool:
        nonlocal finish_reason, model_name, usage
        if cancel_check is not None:
            cancel_check()
        line = (raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) else str(raw_line)).strip()
        if not line or line.startswith(":"):
            return False
        if not line.startswith("data:"):
            return False
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            return True
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            return False
        if not isinstance(chunk, dict):
            return False
        if chunk.get("model"):
            model_name = str(chunk["model"])
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if choices and isinstance(choices[0], dict):
            c0 = choices[0]
            if c0.get("finish_reason"):
                finish_reason = str(c0["finish_reason"])
            delta = c0.get("delta") or c0.get("message") or {}
            if isinstance(delta, dict):
                piece = _content_text(delta) if isinstance(delta.get("content"), list) else delta.get("content")
                if isinstance(piece, str) and piece:
                    content_parts.append(piece)
                rc = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(rc, str) and rc:
                    reasoning_parts.append(rc)
        return False

    if lines is not None:
        for line in lines:
            if consume_line(line):
                break
    else:
        for raw_line in response:
            if consume_line(raw_line):
                break

    message: dict[str, Any] = {"content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    return {
        "model": model_name,
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


@dataclass
class LLMResult:
    text: str
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    attempts: int = 1
    finish_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "attempts": self.attempts,
        }


class LLMClient:
    """Chat completions against an OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: int = 180,
        max_attempts: int = 3,
        effort: str = "low",
    ) -> None:
        if not (base_url and model) or (not api_key and not is_local_base_url(base_url)):
            raise LLMError(
                "LLM_BASE_URL, LLM_API_KEY and LLM_MODEL must all be set "
                "(in the Settings panel or .env; API key is optional for localhost)."
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.is_local = is_local_base_url(self.base_url)
        self.timeout = timeout
        self.max_attempts = max_attempts
        # The tutor's jobs are extraction and short generation, not puzzle
        # solving; "low" keeps them fast. Raised per-call for the harder ones.
        self.effort = effort
        self._supports_effort = True
        self._supports_json_mode = True
        self._supports_stream_options = True
        self._is_reasoning_model = False
        self._max_tokens_cap: int | None = None

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1200,
        json_mode: bool = False,
        temperature: float = 0.2,
        effort: str | None = None,
        timeout: int | None = None,
        attempts: int | None = None,
        stream: bool = False,
        cancel_check: Callable[[], None] | None = None,
    ) -> LLMResult:
        """Run one completion, with SSE streaming, 400 parameter fallback, and retries."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode and self._supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
        chosen_effort = effort if effort is not None else ("" if self.is_local else self.effort)
        if chosen_effort and chosen_effort != "none" and self._supports_effort:
            payload["reasoning_effort"] = chosen_effort
        if stream:
            payload["stream"] = True
            if self._supports_stream_options:
                payload["stream_options"] = {"include_usage": True}

        headers = {
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "Accept": "text/event-stream, application/json" if stream else "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        max_cap = self._max_tokens_cap or _MAX_BUDGET
        initial_budget = max_tokens
        if self._is_reasoning_model:
            floor = 2400 if (timeout is not None and timeout <= 60) else 12000
            initial_budget = min(max_cap, max(max_tokens * 2, floor))
        budget = min(initial_budget, max_cap)
        last_error: Exception | None = None
        last_status: int | None = None
        last_fatal = False
        total_attempts = attempts or self.max_attempts
        attempts_made = 0
        budget_escalations = 0
        param_adjustments = 0
        max_escalations = 1 if (timeout is not None and timeout <= 60) else 2

        attempt = 0
        while attempt < total_attempts:
            if cancel_check is not None:
                cancel_check()
            attempt += 1
            attempts_made += 1
            body = json.dumps({**payload, "max_tokens": budget}).encode()
            started = time.perf_counter()
            try:
                request = urllib.request.Request(
                    f"{self.base_url}/chat/completions",
                    data=body,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    data = _read_completion_response(response, cancel_check=cancel_check)
                latency_ms = int((time.perf_counter() - started) * 1000)

                choice = data["choices"][0]
                message = choice.get("message") or {}
                finish = choice.get("finish_reason") or ""
                raw_content = _content_text(message)
                had_inline_think = "<think" in raw_content.lower()
                content = _strip_thinking(raw_content)
                has_reasoning = bool(message.get("reasoning_content")) or had_inline_think

                usage = data.get("usage") or {}
                details = usage.get("completion_tokens_details") or {}
                reasoning = int(details.get("reasoning_tokens") or 0)
                if reasoning > 0 or has_reasoning:
                    self._is_reasoning_model = True
                prompt_details = usage.get("prompt_tokens_details") or {}
                cached_tokens = int(
                    prompt_details.get("cached_tokens")
                    or usage.get("prompt_cache_hit_tokens")
                    or 0
                )

                max_cap = self._max_tokens_cap or _MAX_BUDGET
                if (
                    not content
                    and budget < max_cap
                    and budget_escalations < max_escalations
                    and (finish == "length" or reasoning > 0 or has_reasoning)
                ):
                    budget = min(max(budget * 3, 2000), max_cap)
                    budget_escalations += 1
                    attempt -= 1  # budget escalation does not consume a network retry
                    log.warning(
                        "LLM %s returned empty content after reasoning (finish=%r, reasoning=%d); escalating max_tokens to %d",
                        self.model, finish, reasoning, budget,
                    )
                    last_error = LLMError(
                        f"empty content after {reasoning} reasoning tokens; retrying at budget {budget}"
                    )
                    continue

                if not content:
                    last_error = LLMError(
                        f"model returned no content (finish_reason={finish!r}, "
                        f"reasoning_tokens={reasoning}, keys={sorted(message)})"
                    )
                    log.warning("LLM %s attempt %d/%d: %s", self.model, attempt, total_attempts, last_error)
                    if attempt < total_attempts:
                        time.sleep(0.8 * attempt)
                        continue
                    raise last_error

                return LLMResult(
                    text=content,
                    model=data.get("model") or self.model,
                    latency_ms=latency_ms,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    reasoning_tokens=reasoning,
                    cached_tokens=cached_tokens,
                    attempts=attempts_made,
                    finish_reason=finish,
                )
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", "replace")
                low = detail.lower()
                # Auto-adapt on HTTP 400/422 caused by optional params or output caps.
                if exc.code in (400, 422) and param_adjustments < 3:
                    adjusted = False
                    if "reasoning_effort" in payload and ("reasoning" in low or "effort" in low or "unknown" in low or "unrecognized" in low):
                        payload.pop("reasoning_effort", None)
                        self._supports_effort = False
                        adjusted = True
                        log.warning("LLM %s rejected reasoning_effort (HTTP %d); retrying without it", self.model, exc.code)
                    elif "response_format" in payload and ("response_format" in low or "json_object" in low):
                        payload.pop("response_format", None)
                        self._supports_json_mode = False
                        adjusted = True
                        log.warning("LLM %s rejected response_format (HTTP %d); retrying without it", self.model, exc.code)
                    elif "max_tokens" in low and budget > 2048:
                        m = _MAX_TOKENS_CAP_RE.search(detail)
                        parsed_cap = int(m.group(1)) if m else 0
                        new_cap = parsed_cap if 1024 <= parsed_cap < budget else (8192 if budget > 8192 else 4096)
                        self._max_tokens_cap = new_cap
                        budget = min(budget, new_cap)
                        adjusted = True
                        log.warning("LLM %s rejected max_tokens (HTTP %d); capping to %d", self.model, exc.code, budget)
                    elif "stream_options" in payload and ("stream_options" in low or "include_usage" in low):
                        payload.pop("stream_options", None)
                        self._supports_stream_options = False
                        adjusted = True
                        log.warning("LLM %s rejected stream_options (HTTP %d); retrying without it", self.model, exc.code)
                    elif payload.get("stream") and "stream" in low:
                        payload.pop("stream", None)
                        payload.pop("stream_options", None)
                        headers["Accept"] = "application/json"
                        adjusted = True
                        log.warning("LLM %s rejected stream=true (HTTP %d); retrying non-streaming", self.model, exc.code)
                    if adjusted:
                        param_adjustments += 1
                        attempt -= 1
                        continue

                last_status = exc.code
                last_fatal = exc.code in (400, 401, 403, 404, 422)
                last_error = LLMError(f"HTTP {exc.code}: {detail}", status_code=exc.code, fatal=last_fatal)
                log.warning("LLM %s HTTP %d: %s", self.model, exc.code, detail)
                if exc.code not in _RETRYABLE:
                    break
            except LLMError:
                raise
            except Exception as exc:  # network, timeout, malformed JSON, cancellation
                if type(exc).__name__ == "JobCancelled":
                    raise
                last_error = exc
                log.warning("LLM %s attempt %d/%d error: %s: %s", self.model, attempt, total_attempts, type(exc).__name__, exc)

            if attempt < total_attempts:
                time.sleep(_BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)])

        raise LLMError(
            f"LLM call failed after {attempts_made} attempt(s): {last_error}",
            status_code=last_status,
            fatal=last_fatal,
        )

    def complete_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1200,
        effort: str | None = None,
        timeout: int | None = None,
        attempts: int | None = None,
    ) -> tuple[Any, LLMResult]:
        """Complete and parse JSON, tolerating fences and surrounding prose.

        A cut-off answer is asked for again once with three times the room.
        `finish_reason` says so directly, and truncation is the one parse failure
        worth retrying: the model had more to say than it was allowed, and with a
        reasoning model part of that allowance goes on hidden reasoning before the
        first visible token. A lesson woven from the reader's own writing lost its
        entire vocabulary box this way -- 2600 tokens, and the JSON stopped
        mid-sentence. Note that `complete` already escalates, but only when the
        answer came back *empty*; text that is merely cut short is returned as-is.
        """
        for budget in (max_tokens, min(max_tokens * 3, _MAX_BUDGET)):
            result = self.complete(prompt, system=system, max_tokens=budget, json_mode=True,
                                   effort=effort, timeout=timeout, attempts=attempts)
            cleaned = _FENCE.sub("", result.text).strip()
            parsed = _loads_lenient(cleaned)
            if parsed is not None:
                return parsed, result
            if result.finish_reason != "length":
                break
        raise LLMError(f"could not parse JSON from: {cleaned[:300]!r}")


def _loads_lenient(cleaned: str) -> Any | None:
    """Parse JSON, tolerating a sentence of commentary around it.

    Models wrap JSON in prose even in JSON mode, so the outermost brace or bracket
    span is tried as well as the whole string. ``None`` means it could not be
    parsed at all -- no valid JSON document parses to None, so the caller can use
    it as the failure marker.
    """
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None

