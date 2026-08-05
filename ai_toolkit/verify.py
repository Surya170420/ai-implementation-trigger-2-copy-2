"""Generic two-stage "extract, then validate" LLM pattern.

Several projects need the same shape: one LLM call extracts/compares
structured facts against a schema (mechanical, low-judgment), then a second
LLM call looks at that extraction holistically and makes the actual
call — true/false positive, escalate/don't, approve/reject, whatever the
project's domain needs. Keeping this here (not duplicated per-project) means
prompt-plumbing bugs like "wrong dict key" or "schema/prompt drift" only
need fixing once.

This module is deliberately domain-agnostic: it takes prompts and schemas
from the caller and does not know about rows, xpaths, or data-quality
cases. hygiene_check/investigator.py is the domain-specific wrapper that
supplies hygiene-check's own prompts/schemas/column list; a different
project would write its own thin wrapper the same way.

Usage:
    extraction = extract(prompt, ExtractionSchema, model=..., base_url=...)
    verdict = validate(prompt, VerdictSchema, model=..., base_url=...)

Both are just generate_structured() under a name that documents the role
each call plays in the pattern — kept separate (rather than exposing
generate_structured directly) so call sites read as "extraction step" /
"validation step" instead of two anonymous structured calls, and so the
retry/budget/error-handling behavior for this pattern can diverge from
plain generate_structured later without changing every call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Type, TypeVar

from pydantic import BaseModel

from ai_toolkit.structured import generate_structured

T = TypeVar("T", bound=BaseModel)

@dataclass
class StructuredResult(Generic[T]):
    parsed: T          # Pydantic object
    raw: str           # Raw LLM output

def extract(prompt: str, schema: Type[T], model: str, *, api_key: str | None = None,
            base_url: str | None = None, retries: int = 2, **kwargs) -> StructuredResult[T]:
    """Stage 1: pull structured facts out of raw evidence against `schema`.
    Keep this call mechanical — comparison/extraction, not judgment about
    cause or severity. That belongs in validate().

    api_key is keyword-only (the * above) specifically so call-site argument
    order can never collide with it again: this function used to take
    api_key as the 2nd POSITIONAL parameter (between prompt and schema),
    while every call site passed schema positionally in that same slot and
    api_key as a keyword -- e.g. extract(prompt, ExtractionResult,
    api_key=key, ...) -- which raised "extract() got multiple values for
    argument 'api_key'" because ExtractionResult was landing in api_key's
    positional slot. Keyword-only + a default makes that class of bug
    impossible here, matching how base_url/retries already work.
    """
    parsed, raw = generate_structured(prompt, schema, api_key=api_key, model=model, base_url=base_url,
                               retries=retries, return_raw=True, **kwargs)
    return StructuredResult(parsed=parsed, raw=raw)

def validate(prompt: str, schema: Type[T], model: str, *, api_key: str | None = None,
             base_url: str | None = None, retries: int = 2, **kwargs) -> T:
    """Stage 2: holistic judgment over stage 1's output. This is the call
    that should see everything stage 1 produced at once (not just the parts
    that looked wrong), so it can catch things a field-by-field view misses.

    api_key is keyword-only for the same reason as in extract() above.
    """
    return generate_structured(prompt, schema, api_key=api_key, model=model, base_url=base_url,
                               retries=retries, **kwargs)
