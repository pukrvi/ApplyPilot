"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-2.0-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-2.0-flash",
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )


    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5


def _timeout_seconds() -> float:
    """Request timeout. Raise LLM_TIMEOUT for slow local models."""
    try:
        value = float(os.environ.get("LLM_TIMEOUT", "120"))
        return value if value > 0 else 120.0
    except (TypeError, ValueError):
        return 120.0


def _min_output_tokens() -> int:
    """Floor for max_tokens, for reasoning models.

    Models that emit chain-of-thought before their answer (Qwen3 "thinking",
    gpt-oss reasoning modes, DeepSeek-R1 distills) spend the budget on
    reasoning first. The pipeline asks for as few as 512 tokens on the
    scoring stage, which such a model consumes entirely — returning an
    empty string rather than an error, so the stage fails silently.

    Set LLM_MIN_OUTPUT_TOKENS to raise the floor for every request.
    """
    try:
        value = int(os.environ.get("LLM_MIN_OUTPUT_TOKENS", "0"))
        return max(value, 0)
    except (TypeError, ValueError):
        return 0

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str,
                 fallback: tuple[str, str, str] | None = None) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_timeout_seconds())
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        # (base_url, model, api_key) to switch to if this endpoint is
        # unreachable. Used for local -> cloud failover.
        self._fallback = fallback
        self._failed_over = False

    def _activate_fallback(self, reason: str) -> bool:
        """Switch permanently to the fallback provider. False if none left."""
        if not self._fallback or self._failed_over:
            return False
        base_url, model, api_key = self._fallback
        log.warning(
            "LLM endpoint %s unreachable (%s). Falling back to %s (model: %s) "
            "for the rest of this run.",
            self.base_url, reason, base_url, model,
        )
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._is_gemini = base_url.startswith(_GEMINI_COMPAT_BASE)
        self._use_native_gemini = False
        self._failed_over = True
        return True

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""

        # Reasoning models served by LM Studio / vLLM return their chain of
        # thought in `reasoning_content` and leave `content` empty when the
        # token budget runs out mid-thought. Silently returning "" makes every
        # downstream stage look like a parse failure, so name the real cause.
        if not content.strip():
            reasoning = choice["message"].get("reasoning_content") or ""
            usage = data.get("usage", {}) or {}
            detail = (usage.get("completion_tokens_details") or {})
            if reasoning and choice.get("finish_reason") == "length":
                raise _ReasoningBudgetExhausted(
                    f"Model spent its entire {usage.get('completion_tokens', '?')}-token "
                    f"budget on reasoning ({detail.get('reasoning_tokens', '?')} reasoning "
                    f"tokens) and produced no answer. Raise max_tokens, or use a "
                    f"non-reasoning model for this stage."
                )
        return content

    def _apply_model_tweaks(self, messages: list[dict]) -> list[dict]:
        """Model-specific prompt adjustments for the currently active model."""
        # Qwen3: /no_think skips chain-of-thought on structured extraction.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            content = first.get("content", "")
            if first.get("role") == "user" and not content.startswith("/no_think"):
                return [{**first, "content": f"/no_think\n{content}"}] + messages[1:]
        return messages

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Reasoning models burn the budget on thinking before they answer.
        # Without a floor they return "" instead of failing loudly.
        max_tokens = max(max_tokens, _min_output_tokens())

        original = messages

        for attempt in range(_MAX_RETRIES):
            # Recomputed each attempt: after a failover the model changes, and
            # /no_think is meaningless to anything but Qwen.
            messages = self._apply_model_tweaks(original)
            try:
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden as exc:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except _ReasoningBudgetExhausted as exc:
                if self._activate_fallback("reasoning budget exhausted"):
                    continue
                raise RuntimeError(
                    f"{self.model}: {exc} "
                    f"(set LLM_FALLBACK_URL or a cloud key to fail over automatically)"
                ) from exc

            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError) as exc:
                # Local server down / not started. Fail over to the cloud
                # provider if one is configured, otherwise report clearly.
                if self._activate_fallback(type(exc).__name__):
                    continue
                raise RuntimeError(
                    f"Cannot reach LLM endpoint {self.base_url} ({type(exc).__name__}). "
                    "If this is LM Studio, check the server is started and the URL "
                    "ends in /v1. Set GEMINI_API_KEY to fail over automatically."
                ) from exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                # A local server that is up but has no model loaded returns
                # 404/400 rather than refusing the connection.
                if resp.status_code in (400, 404) and not self._is_gemini:
                    if self._activate_fallback(f"HTTP {resp.status_code}"):
                        continue
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    # Quota exhausted (not just per-minute throttling) — no
                    # amount of waiting helps, so switch providers now.
                    body = (resp.text or "").lower()
                    if ("quota" in body or "billing" in body) and self._fallback:
                        if self._activate_fallback(f"HTTP {resp.status_code} quota exhausted"):
                            continue

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _ReasoningBudgetExhausted(Exception):
    """Model produced only chain-of-thought and no answer within max_tokens."""


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def _detect_fallback(active_base_url: str) -> tuple[str, str, str] | None:
    """Provider to fail over to when the active one becomes unusable.

    Works in both directions:
      cloud primary -> local, when quota is exhausted or the API is down
      local primary -> cloud, when the local server is unreachable

    Cloud->local uses LLM_FALLBACK_URL (and LLM_FALLBACK_MODEL). Local->cloud
    uses whichever API key is configured.
    """
    is_cloud = active_base_url.startswith(
        ("https://generativelanguage.googleapis.com", "https://api.openai.com")
    )

    if is_cloud:
        # Fail over to a local endpoint if one is declared.
        fallback_url = os.environ.get("LLM_FALLBACK_URL", "")
        if fallback_url:
            return (fallback_url.rstrip("/"),
                    os.environ.get("LLM_FALLBACK_MODEL", "local-model"),
                    os.environ.get("LLM_API_KEY", ""))
        return None

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        return (_GEMINI_COMPAT_BASE,
                os.environ.get("LLM_FALLBACK_MODEL", "gemini-flash-latest"),
                gemini_key)

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        return ("https://api.openai.com/v1",
                os.environ.get("LLM_FALLBACK_MODEL", "gpt-4o-mini"),
                openai_key)

    return None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        fallback = _detect_fallback(base_url)
        if fallback:
            log.info("LLM provider: %s  model: %s  (fallback: %s)",
                     base_url, model, fallback[1])
        else:
            log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key, fallback=fallback)
    return _instance
