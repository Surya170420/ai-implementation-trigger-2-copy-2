"""Prompt templates as files with {{ variable }} slots.

Keeping prompts out of Python code means they can be reviewed and tuned
without touching logic. Templates use {{ name }} placeholders (double braces,
so JSON examples inside the template are untouched).
"""

from __future__ import annotations

import re
from pathlib import Path

_SLOT = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def load(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def render(template: str, **variables) -> str:
    """Fill {{ name }} slots. Raises KeyError listing any missing variable."""
    missing = [m for m in _SLOT.findall(template) if m not in variables]
    if missing:
        raise KeyError(f"prompt template is missing variables: {missing}")
    return _SLOT.sub(lambda m: str(variables[m.group(1)]), template)


def render_file(path: str | Path, **variables) -> str:
    return render(load(path), **variables)
