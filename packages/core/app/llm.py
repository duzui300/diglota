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
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

# Ceiling for automatic budget escalation. Above this a single attempt usually
# either answers or is genuinely stuck. Set from measurement: weaving a passage
# into Spanish is a long generation, and the model spends *more* on hidden
# reasoning than on the answer -- one observed failure burned 16,000 reasoning
# tokens and emitted nothing at all with finish_reason="length".
_MAX_BUDGET = 32_000

_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524, 529}

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


@dataclass
class LLMResult:
    text: str
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    attempts: int = 1
    finish_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
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
        if not (base_url and api_key and model):
            raise LLMError(
                "LLM_BASE_URL, LLM_API_KEY and LLM_MODEL must all be set "
                "(D:/aislop/.env or this project's .env)."
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts
        # The tutor's jobs are extraction and short generation, not puzzle
        # solving; "low" keeps them fast. Raised per-call for the harder ones.
        self.effort = effort

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
    ) -> LLMResult:
        """Run one completion.

        ``timeout`` and ``attempts`` are per-call on purpose. A word gloss is an
        interactive request -- the reader is waiting on it -- and a weave is a
        batch job nobody is watching. Sharing one 180-second timeout across both
        meant a stalled endpoint could block a click for three attempts, nine
        minutes, on a word the app was only going to show one line of text for.
        """
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
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if effort or self.effort:
            payload["reasoning_effort"] = effort or self.effort

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": _UA,
            "Accept": "application/json",
        }

        budget = max_tokens
        last_error: Exception | None = None
        total_attempts = attempts or self.max_attempts
        attempts = 0

        for attempt in range(1, total_attempts + 1):
            attempts = attempt
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
                    data = json.load(response)
                latency_ms = int((time.perf_counter() - started) * 1000)

                choice = data["choices"][0]
                message = choice.get("message") or {}
                finish = choice.get("finish_reason") or ""
                content = _content_text(message)
                if not content and message.get("reasoning_content"):
                    content = str(message["reasoning_content"]).strip()

                usage = data.get("usage") or {}
                details = usage.get("completion_tokens_details") or {}
                reasoning = int(details.get("reasoning_tokens") or 0)

                if not content and budget < _MAX_BUDGET and (finish == "length" or reasoning > 0):
                    budget = min(max(budget * 3, 2000), _MAX_BUDGET)
                    last_error = LLMError(
                        f"empty content after {reasoning} reasoning tokens; retrying at budget {budget}"
                    )
                    continue

                if not content:
                    # Empty with no explanation is the failure that costs the
                    # most time to diagnose later, so record what came back.
                    last_error = LLMError(
                        f"model returned no content (finish_reason={finish!r}, "
                        f"reasoning_tokens={reasoning}, keys={sorted(message)})"
                    )
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
                    attempts=attempts,
                    finish_reason=finish,
                )
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", "replace")
                last_error = LLMError(f"HTTP {exc.code}: {detail}")
                if exc.code not in _RETRYABLE:
                    break
            except Exception as exc:  # network, timeout, malformed JSON
                last_error = exc

            if attempt < total_attempts:
                # Back off generously. A weave is minutes of work and the
                # failures worth riding out are transient: a DNS blip mid-import
                # costs one retry here and the whole article if it is not
                # retried. 0.8s was too short to survive one.
                time.sleep(_BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)])

        raise LLMError(f"LLM call failed after {attempts} attempt(s): {last_error}")

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

