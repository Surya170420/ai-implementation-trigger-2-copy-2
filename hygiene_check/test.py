# """Resolve raw crawl HTML into evidence bundles for the investigator.

# Two granularities, chosen by the caller (pipeline.py) per the project's
# policy:

#   build_case_evidence(...)  one bundle for the whole case (few representative
#                              rows) — used for OSA, and for visibility cases
#                              whose failed_count exceeds the per-row cap.
#   build_row_evidence(...)   one bundle PER FAILING ROW — used for visibility
#                              cases at or under the per-row cap, so each row is
#                              checked against its own product's HTML rather
#                              than a representative neighbor's.

# Both reuse the same HTML location/cleaning logic; they differ only in how
# many rows they resolve HTML+fragment for and how the result is shaped.

# This module replaces the per-row `build_evidence` that used to live in
# html_evidence.py: that version read case["samples"], but fingerprint() (see
# ai_toolkit/checks/engine.py) always populates case["sample_rows"], so it
# silently returned an empty list on every real case. Fixed here.
# """

# from __future__ import annotations

# import io
# import re
# import zipfile
# from pathlib import Path
# from itertools import islice
# from typing import Any

# from bs4 import BeautifulSoup
# from lxml import etree, html as lxml_html

# FRAGMENT_MAX_CHARS = 5000

# CAPTCHA_MARKERS = [
#     "Enter the characters you see below",
#     "api-services-support@amazon.com",
#     "To discuss automated access",
# ]

# # Platform-specific selectors for the main product block on OSA/PDP pages.
# # The list is ordered by likelihood. First match is used.
# OSA_MAIN_BLOCK_SELECTORS = {
#     "Amazon": ["div#ppd", "div#dp-container"],
#     "Flipkart": ["div.fWi7J_", 'div[data-id*="PRODUCT"]'],
#     # Add other platforms here
# }


# # --------------------------------------------------------------- html utils

# def clean_html(raw_html: str) -> str:
#     """Strip noise tags so the LLM isn't burning context on scripts/styles."""
#     soup = BeautifulSoup(raw_html, "html.parser")
#     for tag in soup(["script", "style", "noscript", "svg", "link", "meta"]):
#         tag.decompose()
#     return str(soup)


# def page_signals(html: str) -> dict:
#     """Cheap page-level facts computed deterministically."""
#     tree = lxml_html.fromstring(html)
#     title = (tree.findtext(".//title") or "").strip()
#     return {
#         "page_title": title[:300],
#         "page_size_chars": len(html),
#         "search_result_blocks": html.count('data-component-type="s-search-result"'),
#         "captcha_suspected": any(marker in html for marker in CAPTCHA_MARKERS),
#     }


# def fragment_for_code(html: str, product_code: str) -> str | None:
#     """Serialize the result block for one product code (visibility search
#     results carry data-asin; OSA/PDP pages carry #ppd)."""
#     tree = lxml_html.fromstring(html)
#     nodes = tree.xpath(f'//*[@data-asin="{product_code}"]')
#     if not nodes:
#         return None
#     return etree.tostring(nodes[0], pretty_print=True, encoding="unicode")


# def representative_fragment(html: str) -> str | None:
#     """First search-result block — used for case-level (not per-row) evidence."""
#     tree = lxml_html.fromstring(html)
#     nodes = tree.xpath('//*[@data-component-type="s-search-result"]')
#     if not nodes:
#         nodes = tree.xpath("//*[@data-asin][string-length(@data-asin) > 0]")
#     if not nodes:
#         return None
#     return etree.tostring(nodes[0], pretty_print=True, encoding="unicode")


# def find_product_fragment(html_content: str, platform: str, platform_code: str | None) -> str | None:
#     """Finds the most relevant HTML fragment for a product on an OSA/PDP page.

#     1. Tries a list of known, good selectors for the platform.
#     2. If they fail, it falls back to finding the platform_code in the text
#        or attributes and walks up the DOM to find a suitably large container.
#     """
#     soup = BeautifulSoup(html_content, "html.parser")

#     # 1. Try known high-quality selectors first
#     selectors = OSA_MAIN_BLOCK_SELECTORS.get(platform, [])
#     for selector in selectors:
#         node = soup.select_one(selector)
#         if node:
#             return clean_html(str(node))

#     # 2. Fallback: find platform_code and walk up to a good parent
#     if not platform_code:
#         return None

#     # Find any element containing the platform_code in its text or attributes
#     def code_finder(tag):
#         return (
#             tag.name != "script" and
#             (platform_code in tag.get_text(strip=True) or
#              any(platform_code in str(v) for v in tag.attrs.values()))
#         )

#     start_node = soup.find(code_finder)
#     if not start_node:
#         return None

#     # Walk up to find a container that's big enough but not the whole body
#     for p in islice(start_node.parents, 10): # Limit search depth
#         if p.name == 'body': break
#         if len(str(p)) > 2000: # Heuristic for a "main content" block size
#             return clean_html(str(p))

#     return clean_html(str(start_node.parent)) # Last resort


# # ------------------------------------------------------------------ local_zip

# def _resolve_local_zip(html_dir: str | Path, needles: set[str]) -> tuple[str, str] | None:
#     html_dir = Path(html_dir)
#     candidates = sorted(html_dir.glob("*.zip"))
#     for zip_path in candidates:
#         if any(needle in zip_path.name for needle in needles):
#             with zipfile.ZipFile(zip_path) as zf:
#                 inner = zf.namelist()[0]
#                 return zip_path.name, zf.read(inner).decode("utf-8", errors="replace")
#     return None


# # ---------------------------------------------------------------------- minio

# def _minio_client():
#     import common_utils_repository.minio.minio_read as m
#     from minio import Minio
#     endpoint = m.MINIO_DEFAULT_ENDPOINT
#     return Minio(endpoint=endpoint.split("://")[-1],
#                  access_key=m.MINIO_DEFAULT_ACCESS_KEY,
#                  secret_key=m.MINIO_DEFAULT_SECRET_KEY,
#                  region=m.MINIO_DEFAULT_REGION,
#                  secure=endpoint.startswith("https"))


# def _normalize(name: str) -> str:
#     return re.sub(r"\s+", " ", name.replace("+", " ")).strip().lower()


# def _read_zip_html(bucket: str, object_name: str) -> str:
#     from common_utils_repository.minio.minio_read import minio_read
#     data = minio_read(bucket, object_name)
#     with zipfile.ZipFile(io.BytesIO(data)) as zf:
#         return zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")


# def _prefix_path(evidence_cfg: dict, row: dict) -> str:
#     template = evidence_cfg.get(
#         "prefix_template",
#         "layer=bronze/blobstorage/table_name={table}/platform_id={platform_id}/"
#         "date_stamp={date_stamp}/hour_stamp={hour_stamp}/",
#     )
#     return template.format(
#         table=evidence_cfg.get("table", ""),
#         platform_id=row.get("platform_id") or evidence_cfg.get("platform_id", ""),
#         date_stamp=row.get("date_stamp", ""),
#         hour_stamp=row.get("hour_stamp", ""),
#     )


# def _process_html(html: str, row: dict, is_osa: bool) -> str:
#     """Shared logic to extract the correct HTML fragment for OSA vs. Visibility."""
#     if is_osa:
#         fragment = find_product_fragment(html, row.get("platform", ""), row.get("platform_code"))
#         return fragment or ""
#     else:
#         # For Visibility (search page), find the specific product's result block.
#         code = row.get("platform_code")
#         return clean_html(fragment_for_code(html, str(code))) if code else ""


# def _resolve_minio_for_row(evidence_cfg: dict, row: dict, is_osa: bool,
#                             client_cache: dict) -> tuple[str, str, str] | None:
#     """Returns (source_file_name, prefix_path, cleaned_html) for one row, or
#     None if nothing could be found. Caches the minio client and per-prefix
#     object listing across calls so N rows in the same crawl don't repeat
#     the list_objects call."""
#     from common_utils_repository.minio.minio_read import minio_read

#     bucket = evidence_cfg["bucket"]
#     match_field = evidence_cfg.get("match_field", "keyword")
#     prefix = _prefix_path(evidence_cfg, row)

#     # --- 1. Attempt fast, direct fetch first. This is much more efficient than listing all objects.
#     field_val = row.get(match_field)
#     date_s, hour_s = row.get("date_stamp"), row.get("hour_stamp")
#     if field_val and date_s is not None and hour_s is not None:
#         object_name = f"{prefix}{field_val}_{date_s}_{hour_s}.zip"
#         cache_key = f"raw::{object_name}"
#         if cache_key in client_cache:
#             return Path(object_name).name, prefix, clean_html(client_cache[cache_key])
#         try:
#             data = minio_read(bucket, object_name)
#             with zipfile.ZipFile(io.BytesIO(data)) as zf:
#                 html = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
#                 client_cache[cache_key] = html
#                 return Path(object_name).name, prefix, _process_html(html, row, is_osa)
#         except Exception:
#             # Direct fetch failed, will proceed to fallback logic below.
#             pass

#     # --- 2. If direct fetch fails, fallback to listing objects and scanning content (for OSA).
#     if is_osa and row.get("platform_code"):
#         # For OSA, we must scan the content of files, as filenames are not based on platform_code.
#         if "client" not in client_cache:
#             client_cache["client"] = _minio_client()
#         client = client_cache["client"]
#         if prefix not in client_cache:
#             client_cache[prefix] = [o.object_name for o in client.list_objects(bucket, prefix=prefix)]
#         objects = client_cache[prefix]

#         for obj in objects:
#             cache_key = f"raw::{obj}"
#             if cache_key not in client_cache:
#                 client_cache[cache_key] = _read_zip_html(bucket, obj)
#             raw = client_cache[cache_key]
#             if f'id="ppd" data-asin="{row["platform_code"]}"' in raw or f'data-asin="{row["platform_code"]}"' in raw:
#                  soup = BeautifulSoup(raw, "html.parser")
#                  ppd_div = soup.find("div", {"id": "ppd"})
#                  if ppd_div:
#                      return Path(obj).name, prefix, clean_html(str(ppd_div))

#     return None


# # --------------------------------------------------------------------- xpath

# def debug_xpath(html: str, xpaths: list[str]) -> dict | None:
#     """Walk an xpath axis-by-axis and report the last node that matched
#     before it broke — lets the investigator see exactly where drift happened."""
#     tree = lxml_html.fromstring(html)

#     for xpath in xpaths:
#         tokens = re.findall(r'//|/|[^/]+', xpath)
#         if not tokens:
#             continue

#         current_xpath = tokens[0]
#         last_match = tree
#         matched_any = False

#         i = 1
#         while i < len(tokens):
#             axis = tokens[i]
#             node = tokens[i + 1] if i + 1 < len(tokens) else ""
#             current_xpath += axis + node
#             try:
#                 result = tree.xpath(current_xpath)
#             except etree.XPathEvalError:
#                 break
#             if result:
#                 matched_any = True
#                 last_match = result[0]
#             else:
#                 if not matched_any:
#                     break
#                 html_snippet = (last_match.strip() if isinstance(last_match, str)
#                                  else etree.tostring(last_match, encoding="unicode", pretty_print=True))
#                 return {"xpath": xpath, "breakpoint": current_xpath,
#                         "html": html_snippet[:FRAGMENT_MAX_CHARS]}
#             i += 2

#         if matched_any:
#             return None

#     return None


# # --------------------------------------------------------------------- build

# def _is_osa(case: dict) -> bool:
#     return "osa" in case.get("dataset", "").lower()


# def build_row_evidence(evidence_cfg: dict, case: dict) -> list[dict[str, Any]]:
#     """One evidence item PER FAILING ROW, shaped:
#         {row_data, evidence: {source_file, prefix_path, html_available, html_fragment}}
#     Used for visibility cases at/under the per-row cap.
#     """
#     rows = case.get("sample_rows", [])
#     is_osa = _is_osa(case)
#     client_cache: dict = {}
#     items = []

#     for row in rows[:1]:
#         source_file = prefix = html_fragment = None
#         html_available = False

#         try:
#             if evidence_cfg.get("type") == "minio":
#                 resolved = _resolve_minio_for_row(evidence_cfg, row, is_osa, client_cache)
#                 if resolved:
#                     source_file, prefix, html = resolved
#                     html_available = True
#                     html_fragment = html # The resolver now returns the correct fragment directly
#             elif evidence_cfg.get("type") == "local_zip" and evidence_cfg.get("dir"):
#                 needles = {str(v) for v in (row.get("keyword"), row.get("platform_code")) if v}
#                 resolved = _resolve_local_zip(
#                     Path(__file__).parent.parent / evidence_cfg["dir"], needles
#                 )
#                 if resolved:
#                     source_file, html = resolved
#                     prefix = evidence_cfg.get("dir")
#                     html_available = True
#                     code = row.get("platform_code")
#                     html_fragment = (fragment_for_code(html, str(code)) if code else None) \
#                         or representative_fragment(html)
#         except Exception as exc:
#             items.append({
#                 "row_data": row,
#                 "evidence": {"source_file": None, "prefix_path": None,
#                              "html_available": False, "html_fragment": None,
#                              "resolve_error": str(exc)},
#             })
#             continue

#         items.append({
#             "row_data": row,
#             "evidence": {
#                 "source_file": source_file,
#                 "prefix_path": prefix,
#                 "html_available": html_available,
#                 "html_fragment": html_fragment,
#             },
#         })

#     return items


# def build_case_evidence(evidence_cfg: dict, case: dict, max_candidates: int = 15) -> dict:
#     """One aggregate bundle for the whole case (few representative rows).
#     Used for OSA cases, and for visibility cases over the per-row cap.

#     max_candidates caps how many rows we even ATTEMPT to resolve, not just
#     how many resolved sources we keep. A case can have thousands of failing
#     rows (case["sample_rows"] is every failing row, not a sample — see
#     ai_toolkit/checks/engine.py's _samples()); without this cap, a case where
#     most rows fail to resolve (keyword/ASIN not found) would scan the full
#     list — each attempt doing a minio list_objects + zip download — before
#     ever hitting the early-exit at 3 resolved sources. That is what caused
#     multi-hour hangs on large visibility cases. The caller slicing
#     case["sample_rows"][:3] for its OUTPUT does not help: this function was
#     still iterating the full unsliced list internally before that slice
#     ever applied.
#     """
#     rows = case.get("sample_rows", [])[:max_candidates]
#     is_osa = _is_osa(case)
#     client_cache: dict = {}

#     sources, fragments = [], []
#     # cap how many distinct crawls we pull HTML for per case — representative,
#     # not exhaustive, by design for this granularity
#     seen_prefixes: set[str] = set()

#     for row in rows[:1]:
#         try:
#             if evidence_cfg.get("type") == "minio":
#                 resolved = _resolve_minio_for_row(evidence_cfg, row, is_osa, client_cache)
#                 if not resolved:
#                     continue
#                 source_file, prefix, html = resolved
#             elif evidence_cfg.get("type") == "local_zip" and evidence_cfg.get("dir"):
#                 needles = {str(v) for v in (row.get("keyword"), row.get("platform_code")) if v}
#                 resolved = _resolve_local_zip(
#                     Path(__file__).parent.parent / evidence_cfg["dir"], needles
#                 )
#                 if not resolved:
#                     continue
#                 source_file, html = resolved
#                 prefix = evidence_cfg.get("dir")
#             else:
#                 continue
#         except Exception:
#             continue

#         if prefix in seen_prefixes:
#             continue
#         seen_prefixes.add(prefix)

#         code = row.get("platform_code")
#         frag = (fragment_for_code(html, str(code)) if code else None) or representative_fragment(html)
#         if frag:
#             sources.append(source_file)
#             fragments.append(f"<!-- Source: {source_file} -->\n{frag}")

#         if len(sources) >= 3:  # a few representative crawls is enough for a case-level verdict
#             break

#     if not sources:
#         return {"html_available": False}

#     return {
#         "html_available": True,
#         "sources": sources,
#         "fragment": "\n\n".join(fragments),
#     }



import os
import json
from pathlib import Path

# 1. Matches your system logic: goes up 2 levels to find the true project root
BASE_DIR = Path(__file__).resolve().parent.parent
FILE_PATH = BASE_DIR / "out" / "alert_payload.json"

# 2. Check if the file exists at the true system path
if not FILE_PATH.exists():
    raise FileNotFoundError(
        f"\n[Error] System payload file missing.\n"
        f"Looked at path: {FILE_PATH}\n"
        f"Please verify your pipeline has executed and generated this file."
    )

print(f"Successfully located file at: {FILE_PATH}")

with open(FILE_PATH, 'r', encoding="utf-8") as f:
    json_payload = json.loads(f.read())

# # 4. Core Mailer Execution
# from common_utils_repository.mailer import mailer_script_outlook as m
from hygiene_check.html_body.html_render import render_email_html

email_html = render_email_html(json_payload)

BASE_DIR = Path(__file__).resolve().parent
EMAIL_FILE_PATH = BASE_DIR / "html_body" / "email.html"

EMAIL_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

with open(EMAIL_FILE_PATH, "w", encoding="utf-8") as f:
    f.write(email_html)