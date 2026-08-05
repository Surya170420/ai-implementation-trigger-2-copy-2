"""The shared check library.

Two kinds of checks:

Row checks   — signature (df, column, params) -> 
               Null values PASS every row check except not_null/constant, so
               "must exist" and "must look right when it exists" are separate
               rules and one bad cell is not double-counted.

Group checks — signature (df, column, params) -> (ok: bool, detail: str).
               Run once per crawl group (scope: crawl) or per run (scope: run).

Adding a new check = one function here + @register. Every project can then
reference it by name in its expectations YAML.
"""

from __future__ import annotations

import re

import pandas as pd

ROW_CHECKS: dict = {}
GROUP_CHECKS: dict = {}


def register(name: str, kind: str = "row"):
    def wrap(fn):
        (ROW_CHECKS if kind == "row" else GROUP_CHECKS)[name] = fn
        return fn
    return wrap


def _notnull(series: pd.Series) -> pd.Series:
    """Non-null AND not an empty/whitespace string."""
    mask = series.notna()
    if series.dtype == object:
        mask &= series.astype(str).str.strip() != ""
    return mask


# ---------------------------------------------------------------- row checks

@register("not_null")
def not_null(df, column, params):
    return _notnull(df[column])


@register("constant")
def constant(df, column, params):
    return df[column] == params["value"]


@register("regex")
def regex(df, column, params):
    present = _notnull(df[column])
    matches = df[column].astype(str).str.contains(params["pattern"], regex=True, na=False)
    return ~present | matches


@register("range")
def range_check(df, column, params):
    values = pd.to_numeric(df[column], errors="coerce")
    ok = pd.Series(True, index=df.index)
    if "min" in params:
        ok &= values >= params["min"]
    if "max" in params:
        ok &= values <= params["max"]
    return ~_notnull(df[column]) | ok


@register("length")
def length(df, column, params):
    present = _notnull(df[column])
    n = df[column].astype(str).str.len()
    ok = pd.Series(True, index=df.index)
    if "min" in params:
        ok &= n >= params["min"]
    if "max" in params:
        ok &= n <= params["max"]
    return ~present | ok


@register("allowed_values")
def allowed_values(df, column, params):
    return ~_notnull(df[column]) | df[column].isin(params["values"])


@register("compare")
def compare(df, column, params):
    """Cross-field expression, e.g. expr: "sp <= mrp". Rows where the
    expression involves nulls pass — guard with `when` if needed."""
    result = df.eval(params["expr"])
    return result.fillna(True).astype(bool)


@register("freshness")
def freshness(df, column, params):
    present = _notnull(df[column])
    ts = pd.to_datetime(df[column], errors="coerce")
    age_hours = (pd.Timestamp.now() - ts).dt.total_seconds() / 3600
    return ~present | (age_hours <= params["max_age_hours"])

@register("must_null", kind="row")
def must_null(df, column, params):
    return ~_notnull(df[column])


# -------------------------------------------------------------- group checks

@register("share_max", kind="group")
def share_max(df, column, params):
    """No single value (or one specific `value`) may exceed max_share of rows."""
    if len(df) == 0:
        return True, "empty group"
    counts = df[column].value_counts(dropna=False)
    if "value" in params:
        share = counts.get(params["value"], 0) / len(df)
        top_value = params["value"]
    else:
        share = counts.iloc[0] / len(df)
        top_value = counts.index[0]
    ok = share <= params["max_share"]
    return ok, f"value {str(top_value)[:80]!r} covers {share:.0%} of {len(df)} rows (max allowed {params['max_share']:.0%})"


@register("distinct_min", kind="group")
def distinct_min(df, column, params):
    n = df[column].nunique(dropna=True)
    return n >= params["min"], f"{n} distinct values (min {params['min']})"


@register("contiguous", kind="group")
def contiguous(df, column, params):
    values = sorted(pd.to_numeric(df[column], errors="coerce").dropna().astype(int))
    expected = list(range(1, len(values) + 1))
    ok = values == expected
    detail = f"{len(values)} ranks, expected 1..{len(values)} contiguous"
    if not ok and values:
        gaps = sorted(set(expected) - set(values))[:5]
        detail += f", missing e.g. {gaps}"
    return ok, detail


@register("row_count_min", kind="group")
def row_count_min(df, column, params):
    return len(df) >= params["min"], f"{len(df)} rows (min {params['min']})"


@register("contains_all", kind="group")
def contains_all(df: pd.DataFrame, column: str, params: dict) -> tuple[bool, str]:
    """
    Check if the series contains all the specified values.
    This is a group check: if the condition fails, all rows in the group
    are marked as failures.
    """
    series = df[column]
    required_values = set(params["value"])
    present_values = set(series.unique())
    missing = required_values - present_values

    if not missing:
        return True, f"All {len(required_values)} required values are present."
    
    return False, f"Missing required values: {sorted(list(missing))}"
