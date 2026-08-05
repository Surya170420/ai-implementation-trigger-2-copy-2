"""Infytrix AI toolkit — reusable AI components shared across projects.

Modules:
    llm            generate(prompt, model=..., base_url=...) — one function, any provider
    structured     schema-validated LLM output (pydantic) with retry
    prompts        prompt templates as files with {{ variable }} slots
    observability  JSONL log of every LLM call (model, latency, tokens)
    config         YAML config loading
    checks         declarative data-quality rules engine (YAML expectations)
"""

from ai_toolkit.llm import generate
from ai_toolkit.structured import generate_structured

__all__ = ["generate", "generate_structured"]
