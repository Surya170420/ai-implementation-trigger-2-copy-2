"""
extractors/script_extractor.py
Clones/pulls the scraping-repo from Git, fetches the relevant crawler script, 
and uses an AST parser to extract all XPath assignments into a dictionary.
"""

import ast
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Repo Sync
# ─────────────────────────────────────────────────────────────────────────────

def sync_repo(repo_url: str, repo_dir: Path) -> Path:
    """Clone if absent, pull if present. Returns the repo root Path."""
    if not repo_dir.exists():
        logger.info("[GIT] Cloning %s → %s", repo_url, repo_dir)
        subprocess.run(["git", "clone", repo_url, str(repo_dir)], check=True, capture_output=True)
    else:
        logger.info("[GIT] Pulling latest in %s", repo_dir)
        subprocess.run(["git", "-C", str(repo_dir), "pull"], check=True, capture_output=True)
    return repo_dir


# ─────────────────────────────────────────────────────────────────────────────
# Script Retrieval
# ─────────────────────────────────────────────────────────────────────────────

def get_script(platform_name: str, table_name: str, script_map: dict, repo_dir: Path) -> Optional[str]:
    """Look up the crawler script for a given (platform, table) pair."""
    _alias_map = {
        "t_osa_hourly":        "osa_hourly",
        "neo_osa_hourly":      "osa_hourly",
        "t_osa_daily":         "osa_daily",
        "neo_osa":             "osa_daily",
        "t_visibility_hourly": "visibility",
        "neo_visibility":      "visibility",
    }
    
    alias = _alias_map.get(table_name)
    if alias is None:
        logger.error("[SCRIPT] Unknown table alias for %s", table_name)
        return None

    # Uses the YAML-compatible string key format (e.g. "amazon_osa_hourly")
    key = f"{platform_name.lower()}_{alias}"
    rel_path = script_map.get(key)
    
    if rel_path is None:
        logger.warning("[SCRIPT] No mapping found for key %s", key)
        return None

    full_path = repo_dir / rel_path
    if not full_path.exists():
        logger.error("[SCRIPT] File not found: %s", full_path)
        return None

    logger.info("[SCRIPT] Loaded %s", full_path)
    return full_path.read_text(encoding="utf-8", errors="ignore")


# ─────────────────────────────────────────────────────────────────────────────
# AST XPath Extractor
# ─────────────────────────────────────────────────────────────────────────────

class XPathExtractor(ast.NodeVisitor):
    def __init__(self):
        self.assignments = {}

    def visit_Assign(self, node):
        for target in node.targets:
            if isinstance(target, ast.Name):
                xpaths = []
                self.extract_xpaths(node.value, xpaths)
                if xpaths:
                    self.assignments[target.id] = xpaths

    def extract_xpaths(self, node, xpaths):
        # Handles logic like: tree.xpath(...) or tree.xpath(...)
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                self.extract_xpaths(value, xpaths)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "xpath":
                if node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant):
                        xpaths.append(arg.value)
                    elif isinstance(arg, ast.Str):   # Python <3.8 compatibility
                        xpaths.append(arg.s)
        # Handles nested expressions
        elif hasattr(node, "value"):
            self.extract_xpaths(node.value, xpaths)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def repo_handle(platform: str, table_name: str, script_name_map: dict, repo_url: str, repo_dir: Path) -> dict:
    """Syncs repo, fetches target script, and extracts AST XPath assignments."""
    try:
        repo_dir = sync_repo(repo_url, repo_dir)
    except Exception as exc:
        logger.warning("[GIT] Could not sync repo – using cached/absent scripts: %s", exc)

    script = get_script(
        platform_name=platform,
        table_name=table_name,
        script_map=script_name_map,
        repo_dir=repo_dir
    )

    # Guardrail: Prevent AST parsing crash if the script is missing
    if not script:
        logger.error("[AST] No script available to parse. Returning empty assignments.")
        return {}

    tree = ast.parse(script)
    extractor = XPathExtractor()
    extractor.visit(tree)

    logger.info("[AST] Extracted %d variable mappings.", len(extractor.assignments))
    return extractor.assignments