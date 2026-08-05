"""Expectations engine: load YAML rule files, run them over a DataFrame,
emit Failure records, and fingerprint failures into investigation cases.

Rule anatomy (identical for every rule):

    - id: image_not_placeholder      unique name -> reports & fingerprints
      column: product_image_url      column examined (optional for compare/row_count)
      check: regex                   name from the shared library
      params: {pattern: '...'}
      scope: row | crawl | run
      when: "stock_status == 'In Stock'"   optional guard (pandas query;
                                            "x is null"/"is not null" allowed)
      severity: error | warn
      on_fail: investigate | report_only

File-level keys: dataset, platform, crawl_key (columns defining one crawl),
extends (relative path of a base file whose rules are inherited; a child rule
with the same id replaces the base one). "{{ platform }}" inside params is
substituted from the file's platform.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from ai_toolkit.checks.library import GROUP_CHECKS, ROW_CHECKS

# Cap on how many raw row dicts get carried into a case's sample_rows.
# Downstream only ever uses a handful of these (evidence.py takes rows[:1]
# for the actual LLM investigation; investigate()/report display take
# [:3]), so this is a display/debug sample, not the investigated set -- it
# must stay small regardless of how many thousands of rows a rule failed
# on. See _samples() and fingerprint() below for where this is enforced.
SAMPLE_ROWS = 5



@dataclass
class Failure:
    dataset: str
    platform: str
    rule_id: str
    column: str | None
    check: str
    scope: str
    severity: str
    on_fail: str
    description: str
    force_escalate_above: float | None  # per-rule floor override (None = project default)
    crawl_key: dict | None          # which crawl (None for run scope)
    failed_count: int
    total_count: int
    detail: str
    sample_rows: list[dict] = field(default_factory=list)


# ------------------------------------------------------------------ loading

def load_expectations(path: str | Path, root: str | Path | None = None) -> dict:
    """Load an expectations file, resolving `extends` inheritance, then
    substitute {{ platform }} in params (only after the full merge, so base
    files inherit the child's platform value)."""
    path = Path(path)
    spec = _load_raw(path, Path(root) if root else path.parent.parent)
    platform = spec.get("platform", "")
    for rule in spec.get("rules", []):
        for key, value in list(rule.get("params", {}).items()):
            if isinstance(value, str):
                rule["params"][key] = value.replace("{{ platform }}", platform)
    return spec


def _load_raw(path: Path, root: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    if "extends" in spec:
        base = _load_raw(root / spec["extends"], root)
        merged_rules = {r["id"]: r for r in base.get("rules", [])}
        merged_rules.update({r["id"]: r for r in spec.get("rules", [])})
        base.update({k: v for k, v in spec.items() if k not in ("rules", "extends")})
        base["rules"] = list(merged_rules.values())
        spec = base
    return spec


_NULL_SYNTAX = [
    (re.compile(r"(\w+) is not null"), r"\1.notna()"),
    (re.compile(r"(\w+) is null"), r"\1.isna()"),
]


def _apply_when(df: pd.DataFrame, when: str | None) -> pd.DataFrame:
    if not when:
        return df
    for pattern, repl in _NULL_SYNTAX:
        when = pattern.sub(repl, when)
    return df.query(when, engine="python")


# ------------------------------------------------------------------ running

def run_checks(df: pd.DataFrame, spec: dict) -> list[Failure]:
    dataset = spec.get("dataset", "unknown")
    platform = spec.get("platform", "unknown")
    crawl_key = spec.get("crawl_key", [])
    failures: list[Failure] = []

    for rule in spec.get("rules", []):
        scope = rule.get("scope", "row")
        subset = _apply_when(df, rule.get("when"))
        common = dict(
            dataset=dataset, platform=platform, rule_id=rule["id"],
            column=rule.get("column"), check=rule["check"], scope=scope,
            severity=rule.get("severity", "error"),
            on_fail=rule.get("on_fail", "report_only"),
            description=rule.get("description", ""),
            force_escalate_above=rule.get("force_escalate_above"),
        )

        if scope == "row":
            check_fn = ROW_CHECKS[rule["check"]]
            passed = check_fn(subset, rule.get("column"), rule.get("params", {}))
            failed = subset[~passed]
            if len(failed) == 0:
                continue
            for key, crawl_df in _group_by_crawl(failed, crawl_key):
                failures.append(Failure(
                    **common, crawl_key=key,
                    failed_count=len(crawl_df), total_count=len(subset),
                    detail=f"{len(crawl_df)} of {len(subset)} rows failed",
                    sample_rows=_samples(crawl_df),
                ))

        else:  # crawl / run scope
            check_fn = GROUP_CHECKS[rule["check"]]
            groups = _group_by_crawl(subset, crawl_key) if scope == "crawl" else [(None, subset)]
            for key, group_df in groups:
                if len(group_df) == 0:
                    continue
                import traceback
                try:
                    ok, detail = check_fn(group_df, rule.get("column"), rule.get("params", {}))
                except Exception as e:
                    print("=" * 80)
                    print("Check failed")
                    print(f"Check: {rule.get('name')}")
                    print(f"Function: {check_fn.__name__}")
                    print(f"Column: {rule.get('column')}")
                    print(f"Params: {rule.get('params', {})}")
                    print(f"Group shape: {group_df.shape}")
                    print(group_df.head())
                    traceback.print_exc()
                    raise
                if not ok:
                    failures.append(Failure(
                        **common, crawl_key=key,
                        failed_count=len(group_df), total_count=len(group_df),
                        detail=detail, sample_rows=_samples(group_df),
                    ))
    return failures


def _group_by_crawl(df: pd.DataFrame, crawl_key: list[str]):
    keys = [k for k in crawl_key if k in df.columns]
    if not keys or len(df) == 0:
        return [(None, df)]
    return [
        (dict(zip(keys, values if isinstance(values, tuple) else (values,))), group)
        for values, group in df.groupby(keys, dropna=False)
    ]


def _samples(df: pd.DataFrame) -> list[dict]:
    rows = df.head(SAMPLE_ROWS).to_dict(orient="records")
    return [
        {k: (None if pd.isna(v) else v) if not isinstance(v, (list, dict)) else v
         for k, v in row.items()}
        for row in rows
    ]


# ------------------------------------------------------------- fingerprinting

def fingerprint(failures: list[Failure]) -> list[dict]:
    """Group failures into cases: one broken xpath = thousands of identical
    row failures = ONE case. Case key: (dataset, platform, rule_id)."""
    cases: dict[tuple, dict] = {}
    for f in failures:
        key = (f.dataset, f.platform, f.rule_id)  
        case = cases.setdefault(key, {
            "case_id": f"{f.dataset}|{f.platform}|{f.rule_id}",
            "dataset": f.dataset, "platform": f.platform,
            "rule_id": f.rule_id, "column": f.column, "check": f.check, # change
            "scope": f.scope, "severity": f.severity, "on_fail": f.on_fail,
            "description": f.description,
            "force_escalate_above": f.force_escalate_above,
            "failed_count": 0, "crawls_affected": [], "details": [],
            "sample_rows": [],
        })
        case["failed_count"] += f.failed_count
        if f.crawl_key and f.crawl_key not in case["crawls_affected"]:
            case["crawls_affected"].append(f.crawl_key)
        if f.detail not in case["details"]:
            case["details"].append(f.detail)
        # Cap the case-level total too, not just per-crawl-group: a case
        # spanning many crawls (e.g. one broken rule hit across dozens of
        # keywords) would otherwise still accumulate SAMPLE_ROWS * n_crawls
        # rows even though _samples() truncates each individual group.
        room = SAMPLE_ROWS - len(case["sample_rows"])
        if room > 0:
            case["sample_rows"].extend(f.sample_rows[:room])
    return list(cases.values())
