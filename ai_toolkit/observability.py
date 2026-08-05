"""Logging for LLM calls.

Every generate() call appends one JSON line to a log file so you can see,
per project and per run, how many calls were made, to which model, how slow
and how big. Set the destination with AI_TOOLKIT_LOG_FILE (default:
./llm_calls.jsonl in the working directory).
"""

from __future__ import annotations

import datetime
import json
import logging
import os

logger = logging.getLogger("ai_toolkit")


def log_call(**record) -> None:
    record["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    logger.info("llm call: model=%s latency=%.1fs", record.get("model"), record.get("latency_s", -1))
    path = os.environ.get("AI_TOOLKIT_LOG_FILE", "llm_calls.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("could not write llm call log to %s", path)
