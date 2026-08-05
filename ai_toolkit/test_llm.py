"""Generalized LLM call — one function signature for any provider.

The project owner deliberately picks one model per project (runtime parameter).
There is NO automatic fallback here by design: if a project wants
cloud-to-local fallback it writes its own exception handling around generate().

Model naming follows litellm conventions:
    ollama/qwen2.5:7b          local Ollama (pass base_url, e.g. the k8s DNS)
    openai/gpt-4o-mini         OpenAI API   (needs OPENAI_API_KEY env var)
    openai/MiniMax-Text-01     any OpenAI-compatible API via base_url + api_key
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ai_toolkit.observability import log_call

DEFAULT_MODEL = "ollama/qwen2.5:7b"


@dataclass
class LLMResponse:
    text: str
    model: str
    latency_s: float
    usage: dict = field(default_factory=dict)  # prompt_tokens / completion_tokens when the provider reports them


def generate(
    prompt: str,
    model: str = DEFAULT_MODEL,
    base_url: str | None = None,
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: int = 180,
    **kwargs,
) -> LLMResponse:
    """Send a prompt to any LLM and return its text response.

    Args:
        prompt: the user prompt.
        model: litellm-style model name (see module docstring).
        base_url: API endpoint. For Ollama this is the server URL
            (local docker: http://localhost:11434, prod: the k8s DNS name).
        system: optional system prompt.
        temperature: 0.0 = deterministic; raise only for creative tasks.
        max_tokens: cap on response length.
        timeout: seconds before the call errors out.
        **kwargs: passed through to litellm (api_key, top_p, ...).
    """
    import litellm  # lazy: heavy import, only needed when actually calling a model

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    started = time.time()
    response = litellm.completion(
        model=model,
        messages=messages,
        api_base=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        **kwargs,
    )
    latency = time.time() - started

    text = response.choices[0].message.content or ""
    usage = {}
    if getattr(response, "usage", None):
        usage = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
        }

    log_call(model=model, base_url=base_url, latency_s=latency, usage=usage,
             prompt_chars=len(prompt), response_chars=len(text))

    return LLMResponse(text=text, model=model, latency_s=latency, usage=usage)
