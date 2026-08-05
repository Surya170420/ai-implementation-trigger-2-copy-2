"""The AI investigator: takes one fingerprinted case + raw-page evidence,
returns a structured verdict. Only cases with on_fail: investigate get here —
the AI never sees clean rows.

Two-stage flow (extract_columns -> validate_holistically) sits alongside the
original single-call investigate() below. investigate() stays for --no-llm
compatibility and any caller that still wants one case-level verdict;
extract_columns()/validate_holistically() are the per-row/per-case pipeline
described in pipeline.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ai_toolkit import generate_structured
from ai_toolkit.prompts import render_file
from ai_toolkit.verify import extract, validate, StructuredResult
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("OLLAMA_API_KEY")

PROMPT_PATH = Path(__file__).parent / "prompts" / "investigator.md"
EXTRACTOR_PROMPT_PATH = Path(__file__).parent / "prompts" / "extractor.md"
VALIDATOR_PROMPT_PATH = Path(__file__).parent / "prompts" / "validator.md"

# Columns worth checking against raw HTML, per dataset. Deliberately excludes
# pipeline/hardcoded columns (platform, date_stamp, hour_stamp, campaign_id,
# time_stamp) that never come from page markup — see the base expectations
# YAML comments for which columns are pipeline-set vs scraped.
TRACKED_COLUMNS = {
    "t_visibility_hourly": [
        "product_name", "sp", "mrp", "rating", "num_of_rating",
        "tag", "deal", "product_image_url", "absolute_rank", "relative_rank",
    ],
    "t_osa_hourly": [
        "sp", "mrp", "rating", "num_of_rating", "stock_status",
        "seller", "delivery_days", "tag", "deal",
    ],
    "t_osa_hourly_new": [
        "sp", "mrp", "rating", "num_of_rating", "stock_status",
        "seller", "delivery_days", "tag", "deal",
    ],
}


def tracked_columns_for(dataset: str) -> list[str]:
    return TRACKED_COLUMNS.get(dataset, TRACKED_COLUMNS["t_visibility_hourly"])


class Verdict(BaseModel):
    cause: Literal["xpath_drift", "site_change", "genuine_data_issue",
                   "transient_page_issue", "crawl_blocked", "unknown"]
    confidence: float = Field(ge=0, le=1)
    explanation: str
    suggested_xpath: Optional[str] = None
    escalate: bool
    escalation_summary: Optional[str] = None


def investigate(case: dict, evidence: dict, llm_cfg: dict) -> Verdict:
    if evidence.get("html_available"):
        evidence_text = (
            f"source file: {evidence['source']}\n"
            f"page signals: {json.dumps(evidence['signals'], indent=2)}\n"
            f"result block for product code {evidence.get('fragment_product_code') or '(representative)'}:\n"
            f"```html\n{evidence['fragment']}\n```"
        )
        
        # --- NEW: Inject xpath_debug trace if available (PDP/OSA route) ---
        if "xpath_debug" in evidence:
            debug = evidence["xpath_debug"]
            evidence_text += (
                f"\n\nXPATH DEBUG INFO:\n"
                f"- Original XPath: {debug.get('original_xpath')}\n"
                f"- Failed At Axis: {debug.get('failed_at')}\n"
                f"(Note: The HTML snippet above represents the last successfully matched node before the XPath broke.)"
            )

    else:
        evidence_text = "Raw HTML for this crawl is NOT available."

    sample_rows = [
        {k: v for k, v in row.items() if v not in (None, "")}
        for row in case["sample_rows"][:3]
    ]

    prompt = render_file(
        PROMPT_PATH,
        rule_id=case["rule_id"],
        column=case["column"],
        check=case["check"],
        rule_intent=case.get("description") or "(no description provided)",
        rule_description="; ".join(case["details"][:3]),
        dataset=case["dataset"],
        platform=case["platform"],
        extent=f"{case['failed_count']} failing rows across {max(len(case['crawls_affected']), 1)} crawl(s)",
        sample_rows=json.dumps(sample_rows, indent=2, default=str),
        evidence=evidence_text,
    )

    return generate_structured(
        prompt,
        Verdict,
        api_key=api_key,
        model=llm_cfg["model"],
        base_url=llm_cfg.get("base_url"),
        retries=llm_cfg.get("retries", 2),
        timeout=llm_cfg.get("timeout", 300),
        max_tokens=1000,
    )


# ============================================================= two-stage flow

class ColumnExtraction(BaseModel):
    column: str
    html_value: Optional[str] = None
    real_value: Optional[str] = None
    current_xpath: Optional[str] = None   # from the crawler's own source, if one of the candidates matches this column
    working_xpath: Optional[str] = None   # what actually selects html_value in THIS page (may equal current_xpath, or be the fix)
    match: bool



class ExtractionResult(BaseModel):
    columns: list[ColumnExtraction]


class RowVerdict(BaseModel):
    true_positive: bool
    cause: Literal["xpath_drift", "site_change", "genuine_data_issue",
                   "transient_page_issue", "crawl_blocked", "unknown",
                   "legitimate", "not_applicable"]
    confidence: float = Field(ge=0, le=1)
    explanation: str
    current_xpath: Optional[str] = None   # carried from the extraction step for the column that actually broke, so the email can show current vs suggested side by side
    suggested_xpath: Optional[str] = None


def extract_columns(row_data: dict, evidence: dict, candidate_xpaths: dict,
                     dataset: str, llm_cfg: dict) -> StructuredResult[ExtractionResult]:
    """LLM #1: mechanical per-column comparison of HTML vs stored value.
    No judgment about cause here -- that is LLM #2's job."""
    columns = tracked_columns_for(dataset)

    if evidence.get("html_available") and evidence.get("html_fragment"):
        evidence_text = f"```html\n{evidence['html_fragment']}\n```"
    elif evidence.get("html_available") and evidence.get("fragment"):
        evidence_text = f"```html\n{evidence['fragment']}\n```"
    else:
        evidence_text = "Raw HTML for this crawl is NOT available."

    clean_row = {k: v for k, v in row_data.items() if v not in (None, "")}

    prompt = render_file(
        EXTRACTOR_PROMPT_PATH,
        row_data=json.dumps(clean_row, indent=2, default=str),
        columns=", ".join(columns),
        candidate_xpaths=json.dumps(candidate_xpaths, indent=2, default=str) if candidate_xpaths else "(none extracted)",
        evidence=evidence_text,
    )

    return extract(
        prompt,
        ExtractionResult,
        api_key=api_key,
        model=llm_cfg["model"],
        base_url=llm_cfg.get("base_url"),
        retries=llm_cfg.get("retries", 2),
        timeout=llm_cfg.get("timeout", 300),
    )


def validate_holistically(
    case: dict,
    extraction: StructuredResult[ExtractionResult],
    llm_cfg: dict,
) -> RowVerdict:

    prompt = render_file(
        VALIDATOR_PROMPT_PATH,
        rule_id=case["rule_id"],
        rule_intent=case.get("description") or "(no description provided)",
        dataset=case["dataset"],
        platform=case["platform"],
        column_extraction=case["column"],
        row_context=extraction.raw,      # exact LLM #1 output
    )

    verdict = validate(
        prompt,
        RowVerdict,
        api_key=api_key,
        model=llm_cfg["model"],
        base_url=llm_cfg.get("base_url"),
        retries=llm_cfg.get("retries", 2),
        timeout=llm_cfg.get("timeout", 300),
    )

    case_column = case.get("column")
    if case_column:
        for col in extraction.parsed.columns:
            if col.column == case_column and col.current_xpath:
                verdict.current_xpath = col.current_xpath
                break

    return verdict