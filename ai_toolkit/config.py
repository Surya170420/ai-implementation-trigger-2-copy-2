"""YAML config loading for projects.

Each project keeps a config.yaml with its runtime parameters (model name,
endpoints, thresholds). Values can be overridden per environment via
`overrides`, so the same code runs in local docker and on the cluster.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


def load_config(path: str | Path, overrides: dict | None = None) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    cfg = yaml.safe_load(_expand_env(raw)) or {}
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    return cfg


def _expand_env(text: str) -> str:
    """Expand ${VAR_NAME} in the raw YAML text before parsing, so secrets
    (tokens, credentials) live in the environment, never in the file. Missing
    vars raise immediately rather than silently embedding the literal
    "${VAR}" string into a URL or credential."""
    import re

    def _sub(match: "re.Match") -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            raise ValueError(
                f"config references ${{{name}}} but that environment "
                f"variable is not set"
            )
        return value

    return re.sub(r"\$\{(\w+)\}", _sub, text)


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
