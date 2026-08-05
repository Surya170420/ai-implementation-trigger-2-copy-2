"""Schema-validated LLM output.

generate_structured() asks the model for JSON matching a pydantic schema,
parses and validates the reply, and retries with the validation error fed
back to the model when the reply is malformed. Use this whenever the LLM's
answer is consumed by code rather than read by a human.
"""

from __future__ import annotations

import json
import re
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from ai_toolkit.llm import generate

T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> str:
    """Models often wrap JSON in prose or ```json fences — dig it out."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    block = _JSON_BLOCK.search(text)
    if block:
        return block.group(0)
    raise ValueError(f"no JSON object found in model reply: {text[:200]!r}")


def _repair_unescaped_quotes(text: str) -> str:
    """Best-effort fix for the most common small-model JSON failure: a raw
    `"` inside a string value (e.g. a product title containing a quote mark)
    that was never escaped, breaking the parser with "Expecting ',' delimiter"
    partway through the string. Walks the text char by char tracking whether
    we're inside a string; when a `"` appears where a structural character
    (`,` `:` `}` `]`) would be expected next but isn't, treat it as a literal
    quote and escape it instead of ending the string there. Not a full JSON5
    parser — just enough to recover the single-bad-quote case that dominates
    these errors, without silently accepting truly broken output."""
    out = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and in_string and i + 1 < n:
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                out.append(ch)
            else:
                # Peek ahead past whitespace: a real closing quote is
                # followed by , : } ] or end of string. Anything else means
                # this quote was a literal character inside the value.
                j = i + 1
                while j < n and text[j] in " \t\n\r":
                    j += 1
                next_ch = text[j] if j < n else ""
                if next_ch in (",", ":", "}", "]", ""):
                    in_string = False
                    out.append(ch)
                else:
                    out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _try_close_truncated_json(text: str) -> str | None:
    """Best-effort recovery for a reply cut off mid-object by max_tokens.
    Finds the first '{', then walks forward tracking string/escape state and
    brace/bracket depth; if it ends inside an open string, closes that
    string, then appends whatever closing braces/brackets are needed to
    balance what was opened. Returns None if there's no opening '{' at all
    (nothing to recover) or the result still doesn't parse -- this is a
    cheap attempt to avoid a retry round trip, not a substitute for one."""
    start = text.find("{")
    if start == -1:
        return None
    snippet = text[start:]

    in_string = False
    escape = False
    depth_stack: list[str] = []
    for ch in snippet:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth_stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if depth_stack:
                depth_stack.pop()

    repaired = snippet
    if in_string:
        repaired += '"'
    repaired += "".join(reversed(depth_stack))
    return repaired


def generate_structured(
    prompt: str,
    schema: Type[T],
    model: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    system: str | None = None,
    retries: int = 2,
    return_raw: bool = False,
    **kwargs,
) -> T | tuple[T, str]:
    """Return an instance of `schema` produced by the model.

    Uses Ollama's schema-constrained decoding (the `format` param on
    client.chat, threaded through generate()/**kwargs) so the model is
    grammar-constrained to emit valid JSON matching `schema` directly --
    it can't wrap the reply in prose/fences or invent a value outside an
    enum's Literal options (e.g. a `cause` field emitting stray text
    instead of one of the defined causes). The repair helpers above stay
    as the fallback for the rare case a provider ignores `format` or a
    retry still comes back malformed -- not the primary path anymore.

    Raises ValueError after `retries` failed attempts, with the last error.
    """
    schema_dict = schema.model_json_schema()
    schema_json = json.dumps(schema_dict, indent=2)
    full_prompt = (
        f"{prompt}\n\n"
        f"Respond with ONLY a JSON object (no prose, no markdown fences) "
        f"that is valid against this JSON schema:\n{schema_json}"
    )

    last_error = None
    for attempt in range(retries + 1):
        reply = generate(prompt=full_prompt, 
                         model=model,
                         api_key=api_key, 
                         base_url=base_url,
                         system=system, 
                         format=schema_dict,
                         **kwargs)

        if not reply.text.strip():
            last_error = ValueError(
                "model returned an empty reply (often means the response was "
                "truncated before any content was written -- consider raising "
                "max_tokens, or the prompt/evidence may be too large)"
            )
            full_prompt = (
                f"{prompt}\n\nYour previous reply was empty. Respond with a "
                f"SHORT JSON object only, truncating any long text fields, "
                f"for this schema:\n{schema_json}"
            )
            continue

        try:
            raw_json = _extract_json(reply.text)
        except ValueError as exc:
            # reply.text was non-empty but never contained a complete {...} --
            # almost always max_tokens cutting the response off mid-object,
            # not an actually-empty reply. This used to propagate straight
            # out of the function, silently skipping every remaining retry
            # attempt (attempt 1 of 3 would fail and the other 2 never ran).
            # Try a best-effort close of the truncated JSON first -- often
            # recovers a partial-but-complete-enough object without needing
            # another round trip -- then fall back to retrying with a
            # shorter-output prompt like the empty-reply case above.
            repaired = _try_close_truncated_json(reply.text)
            if repaired is not None:
                try:
                    parsed = schema.model_validate(json.loads(repaired))
                    return (parsed, reply.text) if return_raw else parsed
                except (json.JSONDecodeError, ValidationError):
                    pass
            last_error = ValueError(
                f"model reply was truncated before valid JSON completed "
                f"(reply was {len(reply.text)} chars, likely cut off by "
                f"max_tokens): {exc}"
            )
            full_prompt = (
                f"{prompt}\n\nYour previous reply was cut off before it "
                f"finished (too long). Respond again with a SHORTER JSON "
                f"object only -- truncate any long text fields to a few "
                f"words -- for this schema:\n{schema_json}"
            )
            continue

        try:
            parsed = schema.model_validate(json.loads(raw_json))
            return (parsed, reply.text) if return_raw else parsed
        except json.JSONDecodeError as exc:
            # Most common small-model failure: an unescaped quote inside a
            # string value broke the parse partway through. Try a targeted
            # repair before giving up on this attempt.
            try:
                parsed = schema.model_validate(json.loads(_repair_unescaped_quotes(raw_json)))
                return (parsed, reply.text) if return_raw else parsed
            
            except (json.JSONDecodeError, ValidationError):
                pass
            last_error = exc
            full_prompt = (
                f"{prompt}\n\nYour previous reply was invalid: {exc}\n"
                f"Respond again with ONLY a valid JSON object for this schema:\n{schema_json}\n"
                f"Escape every double-quote and newline inside string values."
            )
        except ValidationError as exc:
            last_error = exc
            full_prompt = (
                f"{prompt}\n\nYour previous reply was invalid: {exc}\n"
                f"Respond again with ONLY a valid JSON object for this schema:\n{schema_json}"
            )
    raise ValueError(f"model failed to produce valid {schema.__name__} "
                     f"after {retries + 1} attempts: {last_error}")
