"""Data loading for hygiene-check: CSV fixtures (local dev) or Trino (prod).

Trino mode queries the silver Iceberg tables through the org common-utils
function, one query per dataset+platform, filtered to the last N hours
(config data_source.lookback_hours) or an explicit --date/--hours window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent


def _trino_query(sql: str, cfg: dict) -> pd.DataFrame:
    # module-level indirection so tests can stub Trino without cluster access
    from common_utils_repository.trino.trino_query import trino_query
    return trino_query(sql, True, cfg["host"], cfg["port"],
                       user_name=cfg["user_name"], catalog=cfg["catalog"],
                       schema=cfg["schema"],ssl_enabled=cfg["ssl"])

import datetime

def hours_window(lookback_hours: int, date: str | None = None,
                 hours: list[int] | None = None) -> list[tuple[str, int]]:
    """(date_stamp, hour_stamp) pairs for the run window. Explicit date+hours
    win (backfills/testing); otherwise the last N hours ending now, correctly
    crossing midnight."""
    if date and hours:
        return [(date, h) for h in hours]
    
    now = datetime.datetime.now() 
    pairs = []
    for offset in range(lookback_hours - 1, -1, -1):
        t = now - datetime.timedelta(hours=offset)
        pairs.append((t.strftime("%Y-%m-%d"), t.hour))
    return pairs



def window_clause(pairs: list[tuple[str, int]]) -> str:
    by_date: dict[str, list[int]] = {}
    for date, hour in pairs:
        by_date.setdefault(date, []).append(hour)
    parts = [
        f"(date_stamp = DATE '{date}' AND hour_stamp IN ({', '.join(map(str, sorted(hrs)))}))"
        for date, hrs in by_date.items()
    ]
    return " OR ".join(parts)


def load_entries(cfg: dict, date: str | None = None,
                 hours: list[int] | None = None) -> list[dict]:
    """One entry per dataset+platform: dataset, platform, expectations,
    evidence (config for raw-HTML lookup), df."""
    source = cfg["data_source"]
    if source["mode"] == "csv_fixtures":
        return [{**fx, "evidence": {"type": "local_zip", "dir": fx.get("html_dir")},
                 "df": pd.read_csv(ROOT / fx["csv"])}
                for fx in source["fixtures"]]

    if source["mode"] == "trino":
        target_date = str(date) if date else (str(source["lookback_date"]) if source.get("lookback_date") else None)
        entries = []
        for ds in source["datasets"]:
            is_current_dataset_daily = ds["dataset"].endswith("_daily")

            if is_current_dataset_daily:
                # For daily datasets, only filter by date_stamp
                query_date = target_date if target_date else (datetime.datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                where_window = f"date_stamp = DATE '{query_date}'"
                entry_hour_range = None
                # For daily datasets, 'pairs' should not contain hour info, but we need a date for 'entry["date"]'
                entry_pairs = [(query_date, None)]
            else:
                # For hourly datasets, generate pairs and window clause normally
                entry_pairs = hours_window(source.get("lookback_hours", 4), target_date, hours)
                where_window = window_clause(entry_pairs)
                entry_hour_range = f"({entry_pairs[0][1]}, {entry_pairs[-1][1]})"

            print(f"where window for {ds['dataset']}: {where_window}")
            for platform, expectations in ds["platforms"].items():
                sql = (f"SELECT * FROM {ds['table']} "
                       f"WHERE platform = '{platform}' AND ({where_window})")
                df = _trino_query(sql, source["trino"])
        
                entries.append({
                    "dataset": ds["dataset"], "platform": platform,
                    "date": entry_pairs[0][0], "hour_range": entry_hour_range,
                    "expectations": expectations,
                    "evidence": {**ds.get("evidence", {}),
                                 "platform_id": ds.get("platform_ids", {}).get(platform)},
                    "df": df,
                })
                if is_current_dataset_daily:
                    print(f"[trino] {ds['dataset']}/{platform}: {len(df)} rows for window {entry_pairs[0][0]}")
                else:
                    print(f"[trino] {ds['dataset']}/{platform}: {len(df)} rows for window {entry_pairs[0]}..{entry_pairs[-1]}")
        
        return entries

    raise ValueError(f"unknown data_source.mode: {source['mode']}")
