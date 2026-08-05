"""Guard the pipeline's own LLM spend (not this chat's — see note below) so a
free-tier cap or a mid-run 429 doesn't waste the validation work already
done. Two triggers, either one pauses the run:

  1. a soft call-count cap from config (llm.max_calls_per_run), checked
     before every LLM call
  2. a hard catch of rate-limit/quota errors surfaced by litellm from
     whichever provider is configured

On either trigger, whatever cases/rows have already been fully processed
this run are written to disk (via the caller) and the run exits 0 rather
than raising, so a re-run picks up cleanly instead of starting over.

NOTE: this budgets the pipeline's calls to its OWN configured LLM provider
(local Ollama or a cloud API key set in config.yaml). It has no visibility
into, and cannot pause, usage of THIS chat session — that's a different
context window with no API this code can read.
"""

from __future__ import annotations


class BudgetExceeded(Exception):
    """Raised to unwind the current case/row loop and trigger a checkpoint."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


_RATE_LIMIT_MARKERS = (
    "rate limit", "ratelimiterror", "quota", "429", "unauthorized", "401",
    "insufficient_quota", "resource_exhausted",
)


def is_rate_limit_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


class CallBudget:
    """Call this before every LLM call; raises BudgetExceeded once the soft
    cap is hit so the pipeline can checkpoint instead of erroring out."""

    def __init__(self, max_calls_per_run: int | None):
        self.max_calls_per_run = max_calls_per_run
        self.calls_made = 0

    def spend(self, n: int = 1) -> None:
        if self.max_calls_per_run is not None and self.calls_made >= self.max_calls_per_run:
            raise BudgetExceeded(
                f"soft cap reached: {self.calls_made}/{self.max_calls_per_run} LLM calls this run"
            )
        self.calls_made += n

class ApiRouting:
    def __init__(self, provider_name: str):
        self.provider_name = provider_name