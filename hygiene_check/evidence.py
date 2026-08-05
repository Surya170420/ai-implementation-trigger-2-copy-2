"""Resolve raw crawl HTML into evidence bundles for the investigator.

Two granularities, chosen by the caller (pipeline.py) per the project's
policy:

  build_case_evidence(...)  one bundle for the whole case (few representative
                             rows) — used for OSA, and for visibility cases
                             whose failed_count exceeds the per-row cap.
  build_row_evidence(...)   one bundle PER FAILING ROW — used for visibility
                             cases at or under the per-row cap, so each row is
                             checked against its own product's HTML rather
                             than a representative neighbor's.

Both reuse the same HTML location/cleaning logic; they differ only in how
many rows they resolve HTML+fragment for and how the result is shaped.

This module replaces the per-row `build_evidence` that used to live in
html_evidence.py: that version read case["samples"], but fingerprint() (see
ai_toolkit/checks/engine.py) always populates case["sample_rows"], so it
silently returned an empty list on every real case. Fixed here.
"""

from __future__ import annotations

import io
import re
import math
import zipfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from lxml import etree, html as lxml_html
from collections import Counter

FRAGMENT_MAX_CHARS = 5000

CAPTCHA_MARKERS = [
    "Enter the characters you see below",
    "api-services-support@amazon.com",
    "To discuss automated access",
]


# --------------------------------------------------------------- html utils

def clean_html(raw_html: str) -> str:
    """Strip noise tags so the LLM isn't burning context on scripts/styles."""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "link", "meta"]):
        tag.decompose()
    return str(soup)


def page_signals(html: str) -> dict:
    """Cheap page-level facts computed deterministically."""
    tree = lxml_html.fromstring(html)
    title = (tree.findtext(".//title") or "").strip()
    return {
        "page_title": title[:300],
        "page_size_chars": len(html),
        "search_result_blocks": html.count('data-component-type="s-search-result"'),
        "captcha_suspected": any(marker in html for marker in CAPTCHA_MARKERS),
    }


def clean_html(raw_html: str | None) -> str:
    """Strip noise tags so the LLM isn't burning context on scripts/styles."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "link", "meta", "header", "footer", "nav", "aside"]):
        tag.decompose()
    return str(soup)


def fragment_for_code(html: str, product_code: str) -> str | None:
    """Serialize the result block for one product code (visibility search
    results carry data-asin; OSA/PDP pages carry #ppd)."""
    tree = lxml_html.fromstring(html)
    nodes = tree.xpath(f'//*[@data-asin="{product_code}"]')
    if not nodes:
        return None
    return etree.tostring(nodes[0], pretty_print=True, encoding="unicode")


# ------------------------------------------------------------------ html cleaning
from bs4 import BeautifulSoup, NavigableString

SKIP_TAGS = {
    "script",
    "style",
    "svg",
    "noscript",
    "meta",
    "link",
    "iframe",
    "template",
}


def text_len(node):
    return len(node.get_text(" ", strip=True))


def build_xpath(node):
    """
    Build XPath for a BeautifulSoup Tag.
    """
    path = []

    while node and node.name != "[document]":
        siblings = [
            sib for sib in node.parent.find_all(node.name, recursive=False)
        ] if node.parent else [node]

        if len(siblings) == 1:
            path.append(node.name)
        else:
            idx = siblings.index(node) + 1
            path.append(f"{node.name}[{idx}]")

        node = node.parent

    return "/" + "/".join(reversed(path))


def serialize_node(node):
    """
    Serialize one DOM node.
    """

    txt = node.get_text(" ", strip=True)

    return {
        "tag": node.name,
        "id": node.get("id"),
        "class": node.get("class"),
        "xpath": build_xpath(node),
        "text_length": len(txt),
        "child_count": len(node.find_all(recursive=False)),
        "html_length": len(str(node)),
        "text_preview": txt,
    }


def extract_candidates(html):

    soup = BeautifulSoup(html, "lxml")

    candidates = []

    for node in soup.find_all():

        if node.name in SKIP_TAGS:
            continue

        txt = node.get_text(" ", strip=True)

        if len(txt) < 200:
            continue

        candidates.append(serialize_node(node))

    candidates.sort(
        key=lambda x: x["text_length"],
        reverse=True,
    )

    return candidates



def data_extraction(html_content: str):

    # 1. Dynamic platform code extraction (Frequency engine)
    id_patterns = [r'(?:pid|asin|sku|id|itemid|data-asin)["\']?\s*[=:]\s*["\']?([a-zA-Z0-9]{10,20})', r'/(?:dp|product|p)/([a-zA-Z0-9]{10,20})']
    candidates = [m for p in id_patterns for m in re.findall(p, html_content, re.I) if not m.isdigit()]
    platform_code = Counter(candidates).most_common(1)[0][0] if candidates else "N/A"

    # 2. DOM text cleaning (Preserving footer/reviews)
    soup = BeautifulSoup(html_content, "html.parser")
    for element in soup(["script", "style", "svg", "img", "iframe", "noscript", "header"]):
        element.decompose()

    lines = [line.strip() for line in soup.get_text(separator="\n").splitlines() if line.strip()]

    # Prepend the dynamically caught platform code to the text output
    return f"DYNAMIC_PLATFORM_CODE: {platform_code}\n" + "\n".join(lines)

# ------------------------------------------------------------------ local_zip

def _resolve_local_zip(html_dir: str | Path, needles: set[str]) -> tuple[str, str] | None:
    html_dir = Path(html_dir)
    candidates = sorted(html_dir.glob("*.zip"))
    for zip_path in candidates:
        if any(needle in zip_path.name for needle in needles):
            with zipfile.ZipFile(zip_path) as zf:
                inner = zf.namelist()[0]
                return zip_path.name, zf.read(inner).decode("utf-8", errors="replace")
    return None


# ---------------------------------------------------------------------- minio

def _minio_client():
    import common_utils_repository.minio.minio_read as m
    from minio import Minio
    endpoint = m.MINIO_DEFAULT_ENDPOINT
    return Minio(endpoint=endpoint.split("://")[-1],
                 access_key=m.MINIO_DEFAULT_ACCESS_KEY,
                 secret_key=m.MINIO_DEFAULT_SECRET_KEY,
                 region=m.MINIO_DEFAULT_REGION,
                 secure=endpoint.startswith("https"))


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("+", " ")).strip().lower()


def _read_zip_html(bucket: str, object_name: str) -> str:
    from common_utils_repository.minio.minio_read import minio_read
    data = minio_read(bucket, object_name)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")


def _prefix_path(evidence_cfg: dict, row: dict) -> str:
    # The dataset name is on the row, not in the evidence_cfg slice.
    dataset_name = row.get("dataset", "")
    is_daily = "daily" in dataset_name
    platform = row.get("platform", "").lower()

    if is_daily:
        # Select daily template based on platform, with a default.
        template_key = f"prefix_template_daily_{platform}"
        template = evidence_cfg.get(template_key, evidence_cfg.get("prefix_template_daily"))
    else:
        # Use the standard hourly template.
        template = evidence_cfg.get("prefix_template")

    return template.format(
        table=dataset_name,
        platform_id=row.get("platform_id") or evidence_cfg.get("platform_id", ""),
        date_stamp=row.get("date_stamp", ""),
        hour_stamp=row.get("hour_stamp", ""),
    )


def _process_html(html: str, row: dict, is_osa: bool) -> str:
    """Shared logic to extract the correct HTML fragment for OSA vs. Visibility."""
    if is_osa:
        return data_extraction(html)
    else:
        # For Visibility (search page), find the specific product's result block.
        code = row.get("platform_code")
        return clean_html(fragment_for_code(html, str(code))) if code else ""


def _resolve_minio_for_row(evidence_cfg: dict, row: dict, is_osa: bool,
                            client_cache: dict, is_daily: bool) -> tuple[str, str, str] | None:
    """Returns (source_file_name, prefix_path, cleaned_html) for one row, or
    None if nothing could be found. Caches the minio client and per-prefix
    object listing across calls so N rows in the same crawl don't repeat
    the list_objects call."""
    from common_utils_repository.minio.minio_read import minio_read

    bucket = evidence_cfg["bucket"]
    match_field = evidence_cfg.get("match_field", "keyword")
    prefix = _prefix_path(evidence_cfg, row)

    # --- 1. Attempt fast, direct fetch first. This is much more efficient than listing all objects.
    field_val = row.get(match_field)
    date_s, hour_s = row.get("date_stamp"), row.get("hour_stamp")

    # For daily, we only need date. For hourly, we need both date and hour.
    # Nykaa daily OSA filenames are not based on platform_code, so we must skip direct fetch for it.
    is_nykaa_daily_osa = is_daily and is_osa and row.get("platform", "").lower() == "nykaa"
    is_daily_osa = is_daily and is_osa
    can_direct_fetch = (field_val and date_s is not None and 
                        (is_daily or hour_s is not None) and not is_daily_osa) or is_nykaa_daily_osa


    if can_direct_fetch:
        if is_daily:
            if is_nykaa_daily_osa:
                object_name = f"{prefix}{field_val}_{date_s}_0.zip"
            else:
                object_name = f"{prefix}{field_val}_{date_s}.zip"
        else:
            object_name = f"{prefix}{field_val}_{date_s}_{hour_s}.zip"
        cache_key = f"raw::{object_name}"
        if cache_key in client_cache:
            return Path(object_name).name, prefix, clean_html(client_cache[cache_key])
        try:
            data = minio_read(bucket, object_name)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                html = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
                client_cache[cache_key] = html
                return Path(object_name).name, prefix, _process_html(html, row, is_osa)
        except Exception:
            # Direct fetch failed, will proceed to fallback logic below.
            pass

    # --- 2. If direct fetch fails, fallback to listing objects and scanning content (for OSA).
    if is_osa and row.get("platform_code"):
        # For OSA, we must scan the content of files, as filenames are not based on platform_code.
        if "client" not in client_cache:
            client_cache["client"] = _minio_client()
        client = client_cache["client"]
        if prefix not in client_cache:
            client_cache[prefix] = [o.object_name for o in client.list_objects(bucket, prefix=prefix)]
        objects = client_cache[prefix]

        for obj in objects:
            cache_key = f"raw::{obj}"
            if cache_key not in client_cache:
                client_cache[cache_key] = _read_zip_html(bucket, obj)
            raw = client_cache[cache_key]
            if f'id="ppd" data-asin="{row["platform_code"]}"' in raw or f'data-asin="{row["platform_code"]}"' in raw or row["platform_code"] in raw:
                 soup = BeautifulSoup(raw, "html.parser")
                 ppd_div = soup.find("div", {"id": "ppd"})
                 if ppd_div:
                     return Path(obj).name, prefix, clean_html(str(ppd_div))

    return None


# --------------------------------------------------------------------- xpath

def debug_xpath(html: str, xpaths: list[str]) -> dict | None:
    """Walk an xpath axis-by-axis and report the last node that matched
    before it broke — lets the investigator see exactly where drift happened."""
    tree = lxml_html.fromstring(html)

    for xpath in xpaths:
        tokens = re.findall(r'//|/|[^/]+', xpath)
        if not tokens:
            continue

        current_xpath = tokens[0]
        last_match = tree
        matched_any = False

        i = 1
        while i < len(tokens):
            axis = tokens[i]
            node = tokens[i + 1] if i + 1 < len(tokens) else ""
            current_xpath += axis + node
            try:
                result = tree.xpath(current_xpath)
            except etree.XPathEvalError:
                break
            if result:
                matched_any = True
                last_match = result[0]
            else:
                if not matched_any:
                    break
                html_snippet = (last_match.strip() if isinstance(last_match, str)
                                 else etree.tostring(last_match, encoding="unicode", pretty_print=True))
                return {"xpath": xpath, "breakpoint": current_xpath,
                        "html": html_snippet[:FRAGMENT_MAX_CHARS]}
            i += 2

        if matched_any:
            return None

    return None


# --------------------------------------------------------------------- build

def _is_osa(case: dict) -> bool:
    return "osa" in case.get("dataset", "").lower()

def _is_daily(case: dict) -> bool:
    return "daily" in case.get("dataset", "").lower()


def build_row_evidence(evidence_cfg: dict, case: dict) -> list[dict[str, Any]]:
    """One evidence item PER FAILING ROW, shaped:
        {row_data, evidence: {source_file, prefix_path, html_available, html_fragment}}
    Used for visibility cases at/under the per-row cap.
    """
    rows = case.get("sample_rows", [])
    is_osa = _is_osa(case)
    is_daily = _is_daily(case)
    client_cache: dict = {}
    items = []

    for r in rows[:1]:
        # Add dataset to row for path generation
        row = {**r, "dataset": case.get("dataset", "")}
        source_file = prefix = html_fragment = None
        html_available = False

        try:
            if evidence_cfg.get("type") == "minio":
                resolved = _resolve_minio_for_row(evidence_cfg, row, is_osa, client_cache, is_daily)
                if resolved:
                    source_file, prefix, html = resolved
                    html_available = True
                    html_fragment = html # The resolver now returns the correct fragment directly
            elif evidence_cfg.get("type") == "local_zip" and evidence_cfg.get("dir"):
                needles = {str(v) for v in (row.get("keyword"), row.get("platform_code")) if v}
                resolved = _resolve_local_zip(
                    Path(__file__).parent.parent / evidence_cfg["dir"], needles
                )
                if resolved:
                    source_file, html = resolved
                    prefix = evidence_cfg.get("dir")
                    html_available = True
                    code = row.get("platform_code")
                    html_fragment = fragment_for_code(html, str(code)) if code else None
        except Exception as exc:
            items.append({
                "row_data": row,
                "evidence": {"source_file": None, "prefix_path": None,
                             "html_available": False, "html_fragment": None,
                             "resolve_error": str(exc)},
            })
            continue

        items.append({
            "row_data": row,
            "evidence": {
                "source_file": source_file,
                "prefix_path": prefix,
                "html_available": html_available,
                "html_fragment": html_fragment,
            },
        })

    return items


def build_case_evidence(evidence_cfg: dict, case: dict, max_candidates: int = 15) -> dict:
    """One aggregate bundle for the whole case (few representative rows).
    Used for OSA cases, and for visibility cases over the per-row cap.

    max_candidates caps how many rows we even ATTEMPT to resolve, not just
    how many resolved sources we keep. A case can have thousands of failing
    rows (case["sample_rows"] is every failing row, not a sample — see
    ai_toolkit/checks/engine.py's _samples()); without this cap, a case where
    most rows fail to resolve (keyword/ASIN not found) would scan the full
    list — each attempt doing a minio list_objects + zip download — before
    ever hitting the early-exit at 3 resolved sources. That is what caused
    multi-hour hangs on large visibility cases. The caller slicing
    case["sample_rows"][:3] for its OUTPUT does not help: this function was
    still iterating the full unsliced list internally before that slice
    ever applied.
    """
    rows = case.get("sample_rows", [])[:max_candidates]
    is_osa = _is_osa(case)
    client_cache: dict = {}

    sources, fragments = [], []
    # cap how many distinct crawls we pull HTML for per case — representative,
    # not exhaustive, by design for this granularity
    seen_prefixes: set[str] = set()
    
    for r in rows[:1]:
        # Add dataset to row for path generation
        row = {**r, "dataset": case.get("dataset", "")}
        try:
            if evidence_cfg.get("type") == "minio":
                is_daily = _is_daily(case)
                resolved = _resolve_minio_for_row(evidence_cfg, row, is_osa, client_cache, is_daily)
                if not resolved:
                    continue
                source_file, prefix, html = resolved
            elif evidence_cfg.get("type") == "local_zip" and evidence_cfg.get("dir"):
                needles = {str(v) for v in (row.get("keyword"), row.get("platform_code")) if v}
                resolved = _resolve_local_zip(
                    Path(__file__).parent.parent / evidence_cfg["dir"], needles
                )
                if not resolved:
                    continue
                source_file, html = resolved
                prefix = evidence_cfg.get("dir")
            else:
                continue
        except Exception:
            continue

        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)

        code = row.get("platform_code")
        frag = fragment_for_code(html, str(code)) if code else None
        if frag:
            sources.append(source_file)
            fragments.append(f"<!-- Source: {source_file} -->\n{frag}")

        if len(sources) >= 3:  # a few representative crawls is enough for a case-level verdict
            break

    if not sources:
        return {"html_available": False}

    return {
        "html_available": True,
        "sources": sources,
        "fragment": "\n\n".join(fragments),
    }
